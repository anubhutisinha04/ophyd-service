"""Simulated SR570-style current-amp IOC for the IOS demo.

Serves the PVs at prefix ``XF:23ID2-ES{CurrAmp:N}`` (N = 1, 2, 3) that
the IOS happi entries reference as ``pd_sclr_gain`` / ``aumesh_sclr_gain``
/ ``sample_sclr_gain`` and the matching ``*_decade`` fields. Each channel
exposes:

    * ``Gain:Val-SP``     — gain enum ("1", "2", "5", "10", "20", "50", ...)
    * ``Gain:Decade-SP``  — decade enum ("100 pA/V", "1 nA/V", ...)

Echo-only — caput stores the new value, no dynamics. The IOS happi entries
declare these as ``EpicsSignal(..., string=True)`` so the IOC presents both
PVs as ENUMs (CA serves the string form when ``string=True`` is requested).

The three channels are declared as flat pvproperty's rather than nested
SubGroups because caproto re-expands the SubGroup's combined prefix, which
fails on NSLS-II's unbalanced ``{...}`` prefix style (parent has unclosed
``{``, sub-component closes it).

Phase 2 of the IOS use case (current-amp + EPU echo); paired with
``ioc_ios_epu.py`` and ``ioc_ios_pgm.py``.
"""

import logging

from caproto import ChannelType
from caproto.server import PVGroup, ioc_arg_parser, pvproperty, run


log = logging.getLogger(__name__)


# SR570-style gain steps. NSLS-II's current-amp panel typically exposes these.
GAIN_VALS = ("1", "2", "5", "10", "20", "50", "100", "200", "500")
GAIN_DECADES = (
    "1 pA/V",
    "10 pA/V",
    "100 pA/V",
    "1 nA/V",
    "10 nA/V",
    "100 nA/V",
    "1 uA/V",
    "10 uA/V",
    "100 uA/V",
    "1 mA/V",
)


def _gain_pv(channel: int) -> pvproperty:
    """One Gain:Val-SP enum PV for CurrAmp channel N. Initial value is the
    middle gain (100, idx 6) so an immediate read returns something sensible."""
    return pvproperty(
        value="100",
        name=f":{channel}}}}}Gain:Val-SP",
        enum_strings=GAIN_VALS,
        dtype=ChannelType.ENUM,
    )


def _decade_pv(channel: int) -> pvproperty:
    """One Gain:Decade-SP enum PV for CurrAmp channel N."""
    return pvproperty(
        value="100 pA/V",
        name=f":{channel}}}}}Gain:Decade-SP",
        enum_strings=GAIN_DECADES,
        dtype=ChannelType.ENUM,
    )


class IosCurrAmpIOC(PVGroup):
    """Three SR570 channels at CurrAmp:1 / CurrAmp:2 / CurrAmp:3.

    The happi entries map:
        pd_sclr_*      → CurrAmp:1
        aumesh_sclr_*  → CurrAmp:2
        sample_sclr_*  → CurrAmp:3
    """

    # caproto runs each pvproperty name through str.format(); literal `{` and
    # `}` must be doubled. The name `:1}}Gain:Val-SP` expands to
    # `:1}Gain:Val-SP` which concatenates with the IOC prefix
    # `XF:23ID2-ES{CurrAmp` to produce `XF:23ID2-ES{CurrAmp:1}Gain:Val-SP`.
    pd_gain     = _gain_pv(1)
    pd_decade   = _decade_pv(1)
    aumesh_gain = _gain_pv(2)
    aumesh_decade = _decade_pv(2)
    sample_gain   = _gain_pv(3)
    sample_decade = _decade_pv(3)


def main():
    # caproto runs the prefix through str.format() too; the literal `{` must
    # be doubled. After expansion the prefix is "XF:23ID2-ES{CurrAmp" which
    # combines with each per-PV name (":N}}Gain:...") to produce the final
    # NSLS-II PV names.
    ioc_options, run_options = ioc_arg_parser(
        default_prefix="XF:23ID2-ES{{CurrAmp",
        desc="IOS SR570-style current-amplifier simulation IOC (CurrAmp:1/2/3).",
    )
    ioc = IosCurrAmpIOC(**ioc_options)
    run(ioc.pvdb, **run_options)


if __name__ == "__main__":
    main()
