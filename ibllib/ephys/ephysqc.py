"""
Quality control of raw Neuropixel electrophysiology data.
"""
from pathlib import Path
import logging

import numpy as np
from scipy import signal

from brainbox.core import Bunch
import alf.io
import ibllib.io.spikeglx
from ibllib.ephys import sync_probes
from ibllib.io import spikeglx
import ibllib.dsp as dsp
import ibllib.io.extractors.ephys_fpga as fpga
from ibllib.misc import print_progress

_logger = logging.getLogger('ibllib')

RMS_WIN_LENGTH_SECS = 3
WELCH_WIN_LENGTH_SAMPLES = 1024


def rmsmap(fbin):
    """
    Computes RMS map in time domain and spectra for each channel of Neuropixel probe

    :param fbin: binary file in spike glx format (will look for attached metatdata)
    :type fbin: str or pathlib.Path
    :return: a dictionary with amplitudes in channeltime space, channelfrequency space, time
     and frequency scales
    """
    if not isinstance(fbin, spikeglx.Reader):
        sglx = spikeglx.Reader(fbin)
    rms_win_length_samples = 2 ** np.ceil(np.log2(sglx.fs * RMS_WIN_LENGTH_SECS))
    # the window generator will generates window indices
    wingen = dsp.WindowGenerator(ns=sglx.ns, nswin=rms_win_length_samples, overlap=0)
    # pre-allocate output dictionary of numpy arrays
    win = {'TRMS': np.zeros((wingen.nwin, sglx.nc)),
           'nsamples': np.zeros((wingen.nwin,)),
           'fscale': dsp.fscale(WELCH_WIN_LENGTH_SAMPLES, 1 / sglx.fs, one_sided=True),
           'tscale': wingen.tscale(fs=sglx.fs)}
    win['spectral_density'] = np.zeros((len(win['fscale']), sglx.nc))
    # loop through the whole session
    for first, last in wingen.firstlast:
        D = sglx.read_samples(first_sample=first, last_sample=last)[0].transpose()
        # remove low frequency noise below 1 Hz
        D = dsp.hp(D, 1 / sglx.fs, [0, 1])
        iw = wingen.iw
        win['TRMS'][iw, :] = dsp.rms(D)
        win['nsamples'][iw] = D.shape[1]
        # the last window may be smaller than what is needed for welch
        if last - first < WELCH_WIN_LENGTH_SAMPLES:
            continue
        # compute a smoothed spectrum using welch method
        _, w = signal.welch(D, fs=sglx.fs, window='hanning', nperseg=WELCH_WIN_LENGTH_SAMPLES,
                            detrend='constant', return_onesided=True, scaling='density', axis=-1)
        win['spectral_density'] += w.T
        # print at least every 20 windows
        if (iw % min(20, max(int(np.floor(wingen.nwin / 75)), 1))) == 0:
            print_progress(iw, wingen.nwin)
    return win


def extract_rmsmap(fbin, out_folder=None, force=False, label=''):
    """
    Wrapper for rmsmap that outputs _ibl_ephysRmsMap and _ibl_ephysSpectra ALF files

    :param fbin: binary file in spike glx format (will look for attached metatdata)
    :param out_folder: folder in which to store output ALF files. Default uses the folder in which
     the `fbin` file lives.
    :param force: do not re-extract if all ALF files already exist
    :param label: string or list of strings that will be appended to the filename before extension
    :return: None
    """
    _logger.info(str(fbin))
    sglx = spikeglx.Reader(fbin)
    # check if output ALF files exist already:
    if out_folder is None:
        out_folder = Path(fbin).parent
    else:
        out_folder = Path(out_folder)
    alf_object_time = f'_spikeglx_ephysQcTime{sglx.type.upper()}'
    alf_object_freq = f'_spikeglx_ephysQcFreq{sglx.type.upper()}'
    if alf.io.exists(out_folder, alf_object_time, glob=[label]) and \
            alf.io.exists(out_folder, alf_object_freq, glob=[label]) and not force:
        _logger.warning(f'{fbin.name} QC already exists, skipping. Use force option to override')
        return
    # crunch numbers
    rms = rmsmap(fbin)
    # output ALF files, single precision with the optional label as suffix before extension
    if not out_folder.exists():
        out_folder.mkdir()
    tdict = {'rms': rms['TRMS'].astype(np.single), 'times': rms['tscale'].astype(np.single)}
    fdict = {'power': rms['spectral_density'].astype(np.single),
             'freq': rms['fscale'].astype(np.single)}
    out_time = alf.io.save_object_npy(out_folder, object=alf_object_time, dico=tdict, parts=label)
    out_freq = alf.io.save_object_npy(out_folder, object=alf_object_freq, dico=fdict, parts=label)
    return out_time + out_freq


def qc_session(session_path, dry=False, force=False):
    """
    Wrapper that exectutes QC from a session folder and outputs the results whithin the same folder
    as the original raw data.
    :param session_path: path of the session (Subject/yyyy-mm-dd/number
    :param dry: bool (False) Dry run if True
    :param force: bool (False) Force means overwriting an existing QC file
    :return: None
    """
    efiles = ibllib.io.spikeglx.glob_ephys_files(session_path)
    for efile in efiles:
        if dry:
            print(efile.ap)
            print(efile.lf)
            continue
        if efile.ap and efile.ap.exists():
            extract_rmsmap(efile.ap, out_folder=None, force=force, label=efile.label)
        if efile.lf and efile.lf.exists():
            extract_rmsmap(efile.lf, out_folder=None, force=force, label=efile.label)


def validate_ttl_test(ses_path):
    """
    For a mock session on the Ephys Choice world task, check the sync channels for all
    device properly connected and perform a synchronization if dual probes to check that
    all channels are recorded properly
    :param ses_path: session path
    :return: True if tests pass, errors otherwise
    """
    LEFT_CAMERA_FRATE_HZ = 60
    RIGHT_CAMERA_FRATE_HZ = 150
    BODY_CAMERA_FRATE_HZ = 30
    SYNC_RATE_HZ = 1
    MIN_TRIALS_NB = 10

    ses_path = Path(ses_path)
    if not ses_path.exists():
        return False
    rawsync, sync_map = fpga._get_main_probe_sync(ses_path)
    last_time = rawsync['times'][-1]

    # get upgoing fronts for each
    sync = Bunch({})
    for k in sync_map:
        fronts = fpga._get_sync_fronts(rawsync, sync_map[k])
        sync[k] = fronts['times'][fronts['polarities'] == 1]
    wheel = fpga.extract_wheel_sync(rawsync, chmap=sync_map, save=False)

    right_fr = np.round(1 / np.median(np.diff(sync.right_camera)))
    left_fr = np.round(1 / np.median(np.diff(sync.left_camera)))
    body_fr = np.round(1 / np.median(np.diff(sync.body_camera)))
    _logger.info(f'Right camera frame rate: {right_fr} Hz')
    _logger.info(f'Left camera frame rate: {left_fr} Hz')
    _logger.info(f'Body camera frame rate: {body_fr} Hz')
    # implement some task logic
    assert abs((1 - left_fr / LEFT_CAMERA_FRATE_HZ)) < 0.1
    assert abs((1 - right_fr / RIGHT_CAMERA_FRATE_HZ)) < 0.1
    assert abs((1 - body_fr / BODY_CAMERA_FRATE_HZ)) < 0.1
    # the imec sync is for 3B Probes only
    if sync.get('imec_sync') is not None:
        assert (np.all(1 - SYNC_RATE_HZ * np.diff(sync.imec_sync) < 0.1))
    assert (len(wheel['re_pos']) / last_time > 5)  # minimal wheel action
    assert (len(sync.frame2ttl) / last_time > 0.2)  # minimal wheel action
    assert (len(sync.bpod) > len(sync.audio) > MIN_TRIALS_NB)  # minimal wheel action
    assert (len(sync.bpod) > MIN_TRIALS_NB * 2)
    _logger.info('ALL CHECKS PASSED !')

    # second step is to test that we can make the sync. Assertions are whitin the synch code
    if sync.get('imec_sync') is not None:
        sync_probes.version3B(ses_path, display=False)
    else:
        sync_probes.version3A(ses_path, display=False)

    return True
