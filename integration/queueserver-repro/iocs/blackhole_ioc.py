#!/usr/bin/env python3
"""Catch-all ("black hole") EPICS IOC for the IOS queueserver demo.

The IOS profile collection instantiates ~100 ophyd devices spanning hundreds of
PVs. To open the profile under the RE Manager, every PV a device force-connects
at startup must *resolve*. Rather than run a faithful IOC per device, this IOC
answers Channel Access searches for ANY PV name, fabricating a channel whose
type is inferred from the name (AreaDetector plugin fields, enums, strings, and
otherwise a float). This is the standard bluesky "spoof beamline" technique.

Ported from the demo/ios-nsls2-queueserver phase-1 pod. Added here: an
exclusion set (BLACKHOLE_EXCLUDE_PVS_FILE, one PV name per line) of the exact
PVs served by the realistic per-device IOS IOCs (ioc_ios_*.py). This IOC will
not answer those exact PVs, so the realistic IOCs serve them without a Channel
Access duplicate-PV race — while every OTHER PV (including sub-PVs of the same
devices that the realistic IOCs happen not to serve) still resolves here, so
the whole profile opens quickly.
"""
import os
import re

from caproto import (ChannelChar, ChannelData, ChannelDouble, ChannelEnum,
                     ChannelInteger, ChannelString)
from caproto.server import ioc_arg_parser, run

# AreaDetector plugin type PVs must report a plausible plugin class so ophyd's
# AD device trees instantiate.
PLUGIN_TYPE_PVS = [
    (re.compile(r'image\d:'), 'NDPluginStdArrays'),
    (re.compile(r'Stats\d:'), 'NDPluginStats'),
    (re.compile(r'CC\d:'), 'NDPluginColorConvert'),
    (re.compile(r'Proc\d:'), 'NDPluginProcess'),
    (re.compile(r'Over\d:'), 'NDPluginOverlay'),
    (re.compile(r'ROI\d:'), 'NDPluginROI'),
    (re.compile(r'Trans\d:'), 'NDPluginTransform'),
    (re.compile(r'HDF\d:'), 'NDFileHDF5'),
    (re.compile(r'TIFF\d:'), 'NDFileTIFF'),
    (re.compile(r'SumAll'), 'NDPluginStats'),
]


def _load_excluded_pvs():
    """Exact PV names the realistic IOCs own; this IOC defers to them."""
    path = os.environ.get("BLACKHOLE_EXCLUDE_PVS_FILE", "")
    if not path or not os.path.exists(path):
        return frozenset()
    with open(path, "r", encoding="utf-8") as fh:
        return frozenset(line.strip() for line in fh if line.strip())


EXCLUDE_PVS = _load_excluded_pvs()


class BlackholeDB(dict):
    """A pvdb that claims every PV except the exact ones a realistic IOC owns."""

    def __contains__(self, key):
        return key not in EXCLUDE_PVS

    def __missing__(self, key):
        if key in EXCLUDE_PVS:
            # Owned by a realistic IOC — let Channel Access find it there.
            raise KeyError(key)
        # Collapse common record/field suffixes onto their base PV so a record
        # and its fields share one fabricated channel.
        if key.endswith(('-SP', '-I', '-RB', '-Cmd')):
            base, _, _ = key.rpartition('-')
            return self[base]
        if key.endswith(('_RBV', ':RBV')):
            return self[key[:-4]]
        channel = self[key] = fabricate_channel(key)
        return channel


def fabricate_channel(key):
    """Infer a reasonable channel type from a PV name."""
    if 'PluginType' in key:
        for pattern, val in PLUGIN_TYPE_PVS:
            if pattern.search(key):
                return ChannelString(value=val)
        return ChannelString(value='NDPluginStats')
    if 'ArrayPort' in key or 'PortName' in key:
        return ChannelString(value=key)
    if 'EnableCallbacks' in key or 'BlockingCallbacks' in key or 'WaitForPlugins' in key:
        return ChannelEnum(value=0, enum_strings=['Disabled', 'Enabled'])
    if 'ImageMode' in key:
        return ChannelEnum(value=0, enum_strings=['Single', 'Multiple', 'Continuous'])
    if 'TriggerMode' in key:
        return ChannelEnum(value=0, enum_strings=['Internal', 'External'])
    if 'ArraySize' in key:
        return ChannelData(value=10)
    if key.endswith('.EGU'):
        return ChannelString(value='mm')
    if 'filenumber' in key.lower():
        return ChannelInteger(value=0)
    if 'file' in key.lower() and 'mode' not in key.lower():
        return ChannelChar(value='a' * 250)
    return ChannelDouble(value=0.0)


def main():
    _, run_options = ioc_arg_parser(default_prefix='', desc='IOS demo PV black hole')
    if not run_options.get('interfaces'):
        run_options['interfaces'] = ['127.0.0.1']
    run(BlackholeDB(), **run_options)


if __name__ == '__main__':
    main()
