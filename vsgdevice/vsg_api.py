# -*- coding: utf-8 -*-

# Copyright (c) 2022 Signal Hound
# For licensing information, please see the API license in the software_licenses folder

from ctypes import *
import numpy
from sys import exit

vsglib = CDLL("vsgdevice/libvsg_api.so.1.2.1")


# ---------------------------------- Defines -----------------------------------

VSG_MAX_DEVICES = 8

VSG60_MIN_FREQ = 30.0e6
VSG60_MAX_FREQ = 6.0e9

VSG_MIN_SAMPLE_RATE = 12.5e3
VSG_MAX_SAMPLE_RATE = 54.0e6

VSG_MIN_LEVEL = -120.0
VSG_MAX_LEVEL = 10.0

VSG_MIN_IQ_OFFSET = -1024
VSG_MAX_IQ_OFFSET = 1024

VSG_MIN_TRIGGER_LENGTH = 0.1e-6 # 100ns
VSG_MAX_TRIGGER_LENGTH = 1.0 # 100ms


# --------------------------------- Mappings ----------------------------------

vsgGetAPIVersion = vsglib.vsgGetAPIVersion
vsgGetAPIVersion.restype = c_char_p

vsgOpenDevice = vsglib.vsgOpenDevice
vsgOpenDeviceBySerial = vsglib.vsgOpenDeviceBySerial
vsgCloseDevice = vsglib.vsgCloseDevice
vsgPreset = vsglib.vsgPreset

vsgRecal = vsglib.vsgRecal

vsgAbort = vsglib.vsgAbort

vsgGetSerialNumber = vsglib.vsgGetSerialNumber
vsgGetFirmwareVersion = vsglib.vsgGetFirmwareVersion
vsgGetCalDate = vsglib.vsgGetCalDate
vsgReadTemperature = vsglib.vsgReadTemperature

vsgSetRFOutputState = vsglib.vsgSetRFOutputState
vsgGetRFOutputState = vsglib.vsgGetRFOutputState

vsgSetTimebase = vsglib.vsgSetTimebase
vsgGetTimebase = vsglib.vsgGetTimebase

vsgSetTimebaseOffset = vsglib.vsgSetTimebaseOffset
vsgGetTimebaseOffset = vsglib.vsgGetTimebaseOffset

vsgSetFrequency = vsglib.vsgSetFrequency
vsgGetFrequency = vsglib.vsgGetFrequency

vsgSetSampleRate = vsglib.vsgSetSampleRate
vsgGetSampleRate = vsglib.vsgGetSampleRate

vsgSetLevel = vsglib.vsgSetLevel
vsgGetLevel = vsglib.vsgGetLevel

vsgSetAtten = vsglib.vsgSetAtten

vsgGetIQScale = vsglib.vsgGetIQScale

vsgSetIQOffset = vsglib.vsgSetIQOffset
vsgGetIQOffset = vsglib.vsgGetIQOffset

vsgSetDigitalTuning = vsglib.vsgSetDigitalTuning
vsgGetDigitalTuning = vsglib.vsgGetDigitalTuning

vsgSetTriggerLength = vsglib.vsgSetTriggerLength
vsgGetTriggerLength = vsglib.vsgGetTriggerLength

vsgSubmitIQ = vsglib.vsgSubmitIQ
vsgSubmitIQ.argtypes = [c_int,
                        numpy.ctypeslib.ndpointer(numpy.float32, ndim=1, flags='C'),
                        c_int]
vsgSubmitTrigger = vsglib.vsgSubmitTrigger

vsgFlush = vsglib.vsgFlush
vsgFlushAndWait = vsglib.vsgFlushAndWait

vsgOutputWaveform = vsglib.vsgOutputWaveform
vsgOutputWaveform.argtypes = [c_int,
                              numpy.ctypeslib.ndpointer(numpy.float32, ndim=1, flags='C'),
                              c_int]
vsgRepeatWaveform = vsglib.vsgRepeatWaveform
vsgRepeatWaveform.argtypes = [c_int,
                              numpy.ctypeslib.ndpointer(numpy.float32, ndim=1, flags='C'),
                              c_int]
vsgOutputCW = vsglib.vsgOutputCW
vsgIsWaveformActive = vsglib.vsgIsWaveformActive

vsgGetUSBStatus = vsglib.vsgGetUSBStatus

vsgGetErrorString = vsglib.vsgGetErrorString
vsgGetErrorString.restype = c_char_p


# ---------------------------------- Utility ----------------------------------

def error_check(func):
    def print_status_if_error(*args, **kwargs):
        return_vars = func(*args, **kwargs)
        if "status" not in return_vars.keys():
            return return_vars
        status = return_vars["status"]
        if status != 0:
            print (f"{'Error' if status < 0 else 'Warning'} {status}: {vsg_get_error_string(status)} in {func.__name__}()")
        if status < 0:
            exit()
        return return_vars
    return print_status_if_error


# --------------------------------- Functions ---------------------------------
def vsg_get_API_version():
    return {
        "api_version": vsgGetAPIVersion()
    }

@error_check
def vsg_open_device():
    device = c_int(-1)
    status = vsgOpenDevice(byref(device))
    return {
        "status": status,
        "handle": device.value
    }

@error_check
def vsg_open_device_by_serial(serial_number):
    device = c_int(-1)
    status = vsgOpenDeviceBySerial(byref(device), serial_number)
    return {
        "status": status,
        "handle": device.value
    }

@error_check
def vsg_close_device(device):
    return {
        "status": vsgCloseDevice(device)
    }

@error_check
def vsg_preset(device):
    return {
        "status": vsgPreset(device)
    }

@error_check
def vsg_recal(device):
    return {
        "status": vsgRecal(device)
    }

@error_check
def vsg_abort(device):
    return {
        "status": vsgAbort(device)
    }

@error_check
def vsg_get_serial_number(device):
    serial = c_int(-1)
    status = vsgGetSerialNumber(device, byref(serial))
    return {
        "status": status,
        "serial": serial.value
    }

@error_check
def vsg_get_firmware_version(device):
    version = c_int(-1)
    status = vsgGetFirmwareVersion(device, byref(version))
    return {
        "status": status,
        "version": version.value
    }

@error_check
def vsg_get_cal_date(device):
    last_cal_date = c_uint(-1)
    status = vsgGetCalDate(device, byref(last_cal_date))
    return {
        "status": status,
        "last_cal_date": last_cal_date.value
    }

@error_check
def vsg_read_temperature(device):
    temp = c_float(-1)
    status = vsgReadTemperature(device, byref(temp))
    return {
        "status": status,
        "temp": temp.value
    }

@error_check
def vsg_set_RF_output_state(device, enabled):
    return {
        "status": vsgSetRFOutputState(device, enabled)
    }

@error_check
def vsg_get_RF_output_state(device):
    enabled = c_int(-1)
    status = vsgGetRFOutputState(device, byref(enabled))
    return {
        "status": status,
        "enabled": enabled.value
    }

@error_check
def vsg_set_timebase(device, state):
    return {
        "status": vsgSetTimebase(device, state)
    }

@error_check
def vsg_get_timebase(device):
    state = c_int(-1)
    status = vsgGetTimebase(device, byref(state))
    return {
        "status": status,
        "state": state.value
    }

@error_check
def vsg_set_timebase_offset(device, ppm):
    return {
        "status": vsgSetTimebaseOffset(device, c_double(ppm))
    }

@error_check
def vsg_get_timebase_offset(device):
    ppm = c_double(-1)
    status = vsgGetTimebaseOffset(device, byref(ppm))
    return {
        "status": status,
        "ppm": ppm.value
    }

@error_check
def vsg_set_frequency(device, frequency):
    return {
        "status": vsgSetFrequency(device, c_double(frequency))
    }

@error_check
def vsg_get_frequency(device):
    frequency = c_double(-1)
    status = vsgGetFrequency(device, byref(frequency))
    return {
        "status": status,
        "frequency": frequency.value
    }

@error_check
def vsg_set_sample_rate(device, sample_rate):
    return {
        "status": vsgSetSampleRate(device, c_double(sample_rate))
    }

@error_check
def vsg_get_sample_rate(device):
    sample_rate = c_double(-1)
    status = vsgGetSampleRate(device, byref(sample_rate))
    return {
        "status": status,
        "sample_rate": sample_rate.value
    }

@error_check
def vsg_set_level(device, level):
    return {
        "status": vsgSetLevel(device, c_double(level))
    }

@error_check
def vsg_get_level(device):
    level = c_double(-1)
    status = vsgGetLevel(device, byref(level))
    return {
        "status": status,
        "level": level.value
    }

@error_check
def vsg_set_atten(device, atten):
    return {
        "status": vsgSetAtten(device, atten)
    }

@error_check
def vsg_get_IQ_scale(device):
    iq_scale = c_double(-1)
    status = vsgGetIQScale(device, byref(iq_scale))
    return {
        "status": status,
        "iq_scale": iq_scale.value
    }

@error_check
def vsg_set_IQ_offset(device, i_offset, q_offset):
    return {
        "status": vsgSetIQOffset(device, i_offset, q_offset)
    }

@error_check
def vsg_get_IQ_offset(device):
    i_offset = c_int(-1)
    q_offset = c_int(-1)
    status = vsgGetIQOffset(device, byref(i_offset), byref(q_offset))
    return {
        "status": status,
        "i_offset": i_offset.value,
        "q_offset": q_offset.value
    }

@error_check
def vsg_set_digital_tuning(device, enabled):
    return {
        "status": vsgSetDigitalTuning(device, enabled)
    }

@error_check
def vsg_get_digital_tuning(device):
    enabled = c_int(-1)
    status = vsgGetDigitalTuning(device, byref(enabled))
    return {
        "status": status,
        "enabled": enabled.value
    }

@error_check
def vsg_set_trigger_length(device, seconds):
    return {
        "status": vsgSetTriggerLength(device, c_double(seconds))
    }

@error_check
def vsg_get_trigger_length(device):
    seconds = c_double(-1)
    status = vsgGetTriggerLength(device, byref(seconds))
    return {
        "status": status,
        "seconds": seconds.value
    }

@error_check
def vsg_submit_IQ(device, iq, length):
    return {
        "status": vsgSubmitIQ(device, iq, length)
    }

@error_check
def vsg_submit_trigger(device):
    return {
        "status": vsgSubmitTrigger(device)
    }

@error_check
def vsg_flush(device):
    return {
        "status": vsgFlush(device)
    }

@error_check
def vsg_flush_and_wait(device):
    return {
        "status": vsgFlushAndWait(device)
    }

@error_check
def vsg_output_waveform(device, iq, length):
    return {
        "status": vsgOutputWaveform(device, iq, length)
    }

@error_check
def vsg_repeat_waveform(device, iq, length):
    return {
        "status": vsgRepeatWaveform(device, iq, length)
    }

@error_check
def vsg_output_CW(device):
    return {
        "status": vsgOutputCW(device)
    }

@error_check
def vsg_is_waveform_active(device):
    active = c_int(-1)
    status = vsgIsWaveformActive(device, byref(active))
    return {
        "status": status,
        "active": active.value
    }

@error_check
def vsg_get_USB_status(device):
    return {
        "status": vsgGetUSBStatus(device)
    }

def vsg_get_error_string(status):
    return {
        "error_string": vsgGetErrorString(status)
    }
