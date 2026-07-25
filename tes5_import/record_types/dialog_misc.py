"""Misc converters: SOUN, PACK, WTHR.

All dialog/quest/DIAL/INFO/DLBR/DLVW logic has been moved to
tes5_import.dialog_converter.
"""

import struct

from ..text_reader import get_hex_bytes
from .common import (
    _prefix_path,
    get_float,
    get_formid,
    get_int,
    get_str,
    pack_formid_subrecord,
    pack_obnd,
    pack_record,
    pack_string_subrecord,
    pack_subrecord,
    pack_uint8_subrecord,
    pack_uint32_subrecord,
)


# TES4 SNDX/SNDD flag bits (xEdit wbDefinitionsTES4, SOUN)
_TES4_SND_RANDOM_FREQ_SHIFT = 0x0001
_TES4_SND_LOOP              = 0x0010
_TES4_SND_MENU_SOUND        = 0x0020
_TES4_SND_2D                = 0x0040

# Vanilla Skyrim SOPM constants (verified against references/Skyrim.esm SOPM dump)
_SOPM_2D = 0x000B5183            # SOMDialogue2D — non-attenuating, for menu/2D sounds
_SOPM_ONAM_CHANNELS = bytes.fromhex(
    '646400003232323264000000640064000064000000640064')
# ANAM: unknown[4] minDistance(f32) maxDistance(f32) curve[5] unknown[3]
_SOPM_ANAM_LEAD = bytes.fromhex('809dfa00')   # most common in vanilla (24/69)
_SOPM_ANAM_TAIL = b'\x00\x00\x00'             # most common in vanilla (56/69)
# Standard vanilla falloff curve, shared by every SOMMono*/SOMStereoRad* model
_SOPM_CURVE = bytes((100, 50, 20, 5, 0))


def _build_sopm(writer, min_dist: float, max_dist: float, stereo: bool) -> int:
    """Get-or-create a Sound Output Model with the given attenuation distances.

    Skyrim does not store falloff distances on the sound itself — they live in
    the SOPM the SNDR's ONAM points at (vanilla ships one per distance:
    SOMMono00400, SOMMono03000, SOMMono10000, ...).  Oblivion instead stores the
    distances per-SOUN in SNDX, so we mint a SOPM per distinct distance pair and
    cache it, rather than pinning every sound to a single model.

    Returns the SOPM FormID.
    """
    cache = getattr(writer, '_sopm_cache', None)
    if cache is None:
        cache = writer._sopm_cache = {}
    key = (round(min_dist), round(max_dist), stereo)
    if key in cache:
        return cache[key]

    fid = writer.alloc_formid()
    kind = 'Stereo' if stereo else 'Mono'
    subs = pack_string_subrecord(
        'EDID', f'TES4_SOM{kind}{round(max_dist):05d}_{round(min_dist):05d}')
    # NAM1: Flags(u8) unknown[2] ReverbSend%(u8).  Flag 0x01 = Attenuates With
    # Distance — required, or the sound plays at full volume everywhere.
    subs += pack_subrecord('NAM1', struct.pack('<BHB', 0x01, 0, 30))
    # MNAM: 0 = Uses HRTF (mono), 1 = Defined Speaker Output (stereo)
    subs += pack_uint32_subrecord('MNAM', 1 if stereo else 0)
    if stereo:
        subs += pack_subrecord('ONAM', _SOPM_ONAM_CHANNELS)
    subs += pack_subrecord('ANAM', _SOPM_ANAM_LEAD
                           + struct.pack('<ff', min_dist, max_dist)
                           + _SOPM_CURVE + _SOPM_ANAM_TAIL)
    writer.add_record('SOPM', pack_record('SOPM', fid, 0, subs))
    cache[key] = fid
    return fid


def convert_SOUN(rec: dict, writer=None) -> tuple:
    """SOUN — needs companion SNDR record in TES5.
    Returns (soun_bytes, sndr_bytes_or_None, sndr_formid).

    SOUN order: EDID OBND SDSC
    SNDR order: EDID CNAM GNAM SNAM ANAM[] ONAM LNAM BNAM

    Volume in Skyrim comes from two places, and both must be carried over from
    TES4 or every sound plays far louder than vanilla:
      * SNDR BNAM 'Static Attenuation (db)' — a per-sound volume trim.  Oblivion
        stores the same value in SNDX bytes 8-9 (95% of Oblivion.esm SOUNs set
        it; median 6.6 dB).
      * The SOPM's min/max attenuation distance — how fast the sound falls off
        with distance.  Oblivion stores these in SNDX bytes 0-1.
    """
    subs = b''
    edid = get_str(rec, 'EditorID')
    if edid:
        subs += pack_string_subrecord('EDID', edid)
    subs += pack_obnd()

    # SDSC → link to SNDR
    sndr_fid = 0
    sndr_bytes = None
    filename = get_str(rec, 'FNAM.Filename')
    if filename and writer:
        # SNDX and SNDD hold the same struct; whichever is present wins.
        pfx = 'SNDD' if rec.get('SNDD.MaxAttDist') is not None else 'SNDX'
        tes4_flags = get_int(rec, f'{pfx}.Flags') or 0
        # TES4 stores the distances scaled down: min x5, max x100 (xEdit wbMul).
        min_dist = (get_int(rec, f'{pfx}.MinAttDist') or 0) * 5.0
        max_dist = (get_int(rec, f'{pfx}.MaxAttDist') or 0) * 100.0
        # Static attenuation is a u16 of hundredths of a dB in both games, so it
        # transfers as a raw value with no rescaling.
        static_atten = get_int(rec, f'{pfx}.StaticAttenuation') or 0

        is_2d = bool(tes4_flags & (_TES4_SND_2D | _TES4_SND_MENU_SOUND))

        sndr_fid = writer.alloc_formid()
        sndr_subs = b''
        sndr_edid = f"TES4_{edid}_SNDR" if edid else f"TES4_SOUN_{get_formid(rec, 'FormID'):08X}_SNDR"
        sndr_subs += pack_string_subrecord('EDID', sndr_edid)
        # CNAM = Descriptor Type constant (0x1EEF540A — matches all vanilla SNDR records)
        sndr_subs += pack_uint32_subrecord('CNAM', 0x1EEF540A)
        # GNAM = Category: AudioCategorySFX (FormID 0x000172A1 in Skyrim.esm)
        sndr_subs += pack_formid_subrecord('GNAM', 0x000172A1)
        # ANAM = Sound file path
        sndr_subs += pack_string_subrecord('ANAM', _prefix_path(filename))
        # ONAM = Sound Output Model. Required — CK reports 'Sound Output Model
        # missing' if absent.  2D/menu sounds are not positional, so they take
        # the vanilla non-attenuating model; everything else gets a SOPM built
        # from this sound's own TES4 falloff distances.
        if is_2d or max_dist <= 0:
            onam_fid = _SOPM_2D
        else:
            onam_fid = _build_sopm(writer, min_dist, max_dist, stereo=False)
        sndr_subs += pack_formid_subrecord('ONAM', onam_fid)
        # LNAM = Loop Data struct (4 bytes): byte[0]=Unknown, byte[1]=Looping enum,
        # byte[2]=Unknown, byte[3]=Rumble.  Looping enum: 0x00=None, 0x08=Loop.
        lnam_value = 0x00000800 if (tes4_flags & _TES4_SND_LOOP) else 0
        sndr_subs += pack_subrecord('LNAM', struct.pack('<I', lnam_value))
        # BNAM = Values: FreqShift(S8) FreqVariance(S8) Priority(U8) dbVariance(U8) StaticAttenuation(U16)
        freq_adj = get_int(rec, f'{pfx}.FreqAdj') or 0
        freq_var = 0 if not (tes4_flags & _TES4_SND_RANDOM_FREQ_SHIFT) else 10
        sndr_subs += pack_subrecord(
            'BNAM', struct.pack('<bbBBH', max(-128, min(127, freq_adj)),
                                freq_var, 128, 0, min(65535, static_atten)))
        sndr_bytes = pack_record('SNDR', sndr_fid, 0, sndr_subs)

    if sndr_fid:
        subs += pack_formid_subrecord('SDSC', sndr_fid)

    soun_bytes = pack_record('SOUN', get_formid(rec, 'FormID'), get_int(rec, 'RecordFlags'), subs)
    return soun_bytes, sndr_bytes, sndr_fid


# Skyrim.esm 0x161 'DefaultImageSpaceExterior'.  ONLY a last-resort fallback
# for a weather whose TES4 record carries no HNAM — see _wthr_imgs for why it
# must not be used as the general target.
_DEFAULT_IMGS = 0x00000161

# --- HDR tone mapping: TES4 WTHR.HNAM -> TES5 IMGS.HNAM --------------------
#
# This is the field that decides overall scene exposure, and the two games put
# it in DIFFERENT RECORDS: Oblivion stores HDR per WEATHER (WTHR.HNAM, 14
# floats), Skyrim stores it in an IMAGESPACE that the weather points at
# (WTHR.IMSP -> IMGS.HNAM, 9 floats).  There is no TES5 WTHR field for it.
#
# Pointing every converted weather at the stock 0x161 is NOT a valid
# conversion: 0x161 is one of only two vanilla imagespaces that ship ENAM and
# NO HNAM (the other is 0x160 Interior), so the HDR block is left undefined and
# the scene renders blown-out at every hour.  332 of the 336 IMSP references in
# Skyrim.esm point at imagespaces that DO have an HNAM.  So convert the TES4
# HDR data into a real IMGS per weather.
#
# Field correspondence (xEdit wbDefinitionsTES5 IMGS/HNAM + UESP
# 'Skyrim Mod:Mod File Format/IMGS', whose note names slots 5/6 the
# "target luminance" pair — i.e. exactly TES4's TargetLum/UpperLumClamp):
#
#   TES5 slot            <- TES4 WTHR.HNAM field       units
#   0 Eye Adapt Speed    <- EyeAdaptSpeed              DIFFERENT (see below)
#   1 Bloom Blur Radius  <- BlurRadius                 same (both 0..8)
#   2 Bloom Threshold    <- BrightClamp                same 0..1-ish
#   3 Bloom Scale        <- BrightScale                same 0..3
#   4 Recv Bloom Thresh  <- TargetLum                  same 0..1.2
#   5 White              <- UpperLumClamp              same ~1.0
#   6 Sunlight Scale     <- SunlightDimmer             same 0..2
#   7 Sky Scale          <- (no TES4 source)           vanilla median 0.127
#   8 Eye Adapt Strength <- (no TES4 source)           vanilla median 11.0
#
# EyeAdaptSpeed is the one genuine UNIT change.  Oblivion's is a 0..1 rate
# (`fEyeAdaptSpeed:BlurShaderHDR`, engine default 0.7 — the setting is in
# Oblivion.ini and named in Oblivion.exe at 0xA3E965); Skyrim's spans 7..100
# across the 268 vanilla imagespaces (median 37).  Copying 0.7 into a field
# the engine reads on a 7..100 scale all but freezes eye adaptation, so the
# TES4 0..1 rate is mapped onto the vanilla range.
_TES4_EYE_ADAPT_DEFAULT = 0.7

# Per-field (min, max) observed across the 213 vanilla imagespaces that a
# Skyrim.esm WEATHER actually references.  Interior/dungeon imagespaces are
# excluded: they are half the 268 total and pull the envelope somewhere no
# outdoor weather ever sits.
#
# Every converted value is clamped into this envelope, because a TES4 value
# outside it is not merely unusual — it is degenerate for Skyrim's tonemapper.
# `DefaultWeather`, the weather the engine falls back to for the 57
# worldspaces with no CNAM (Tamriel and every city), ships an ALL-ZERO HNAM in
# Oblivion.esm; copied verbatim that gives White=0 and SunlightScale=0, i.e. a
# zero white point, and the whole scene renders blown out at every hour.
_IMGS_HNAM_RANGES = (
    (15.0, 50.0),    # 0 Eye Adapt Speed
    (0.8, 8.0),      # 1 Bloom Blur Radius
    (0.0, 0.80),     # 2 Bloom Threshold
    (0.0, 7.0),      # 3 Bloom Scale
    (0.2, 1.0),      # 4 Receive Bloom Threshold
    (0.6, 1.075),    # 5 White
    (0.4, 3.85),     # 6 Sunlight Scale
    (0.0, 0.45),     # 7 Sky Scale
    (1.0, 30.0),     # 8 Eye Adapt Strength
)

# TES5 gives a weather FOUR imagespaces, one per time of day, and 59 of the 84
# vanilla weathers (70%) really do use distinct ones — day and night differ a
# lot.  TES4 has a single HDR block per weather with no time axis, so the
# fields TES4 cannot supply are taken from the vanilla PER-SLOT medians
# (measured over all 84 weathers x their 4 slots):
#
#                    Sunrise   Day   Sunset  Night
#   Bloom Threshold    0.375  0.625   0.475  0.375
#   Eye Adapt Speed   37      40     37     45
#   Eye Adapt Strength 15      5      15     20
#   Sky Scale          0.175  0.210   0.200  0.035
#
# Collapsing all four slots onto one imagespace (the first version) gave day
# and night identical tone mapping.
_IMGS_SLOT_NAMES = ('Dawn', 'Day', 'Dusk', 'Night')
_IMGS_SLOT_EYE_ADAPT_STRENGTH = (15.0, 5.0, 15.0, 20.0)
# Eye-adapt speed multiplier relative to the weather's own TES4 rate, so an
# authored TES4 value still drives the result but keeps vanilla's day/night
# shape (medians 37/40/37/45 -> normalised against the 40 day value).
_IMGS_SLOT_EYE_ADAPT_BIAS = (0.925, 1.0, 0.925, 1.125)
# Vanilla also dims Sunlight Scale after dark (per-slot medians
# 1.85/1.90/1.85/1.50); TES4 has one value for the whole day.
_IMGS_SLOT_SUNLIGHT_BIAS = (0.974, 1.0, 0.974, 0.789)

# Sky Scale is the sky's contribution to scene exposure and TES4 has no
# equivalent field.  It tracks how bright the weather's own sky colour is
# (corr +0.29 over 284 vanilla weather/slot pairs): a dark night sky sits at
# ~0.025 while any lit sky sits at ~0.19-0.21.  A flat value washes the day
# sky out to near-white while over-lighting the night.
_IMGS_SKY_SCALE_DARK = 0.025
_IMGS_SKY_SCALE_LIT = 0.20
_IMGS_SKY_LUM_DARK = 40.0     # sky luminance below which the bucket is "dark"

# Bloom threshold decides how much of the frame blooms — LOWER means MORE of
# the image blows out.  TES4's BrightClamp is a different curve and its values
# (median 0.30) sit well under vanilla's day figure, which is what produced
# the excessive bloom.  Blend the authored TES4 value toward the vanilla
# per-slot median so an authored difference still shows without the haze.
_IMGS_SLOT_BLOOM_THRESHOLD = (0.375, 0.625, 0.475, 0.375)
_IMGS_BLOOM_TES4_WEIGHT = 0.35

# Several TES4 HDR fields occupy a DIFFERENT numeric range from the TES5 field
# they feed, so a raw copy lands at (or past) the TES5 ceiling:
#
#   field           TES4 observed   TES5 weather-used   effect of copying raw
#   TargetLum        0.75 .. 1.20    0.20 .. 1.00       pinned at 1.0 -> the
#                                                        WHOLE frame blooms
#   UpperLumClamp    1.00 .. 1.30    0.60 .. 1.075      pinned at the ceiling
#   SunlightDimmer   0.50 .. 2.00    0.40 .. 3.85       usable, but compressed
#                                                        into the dim half
#
# Map each from its TES4 span onto the vanilla span so relative differences
# between weathers survive while absolute values land where Skyrim expects.
# (lo_in, hi_in, lo_out, hi_out)
_IMGS_FIELD_RESCALE = {
    'TargetLum':      (0.75, 1.20, 0.42, 0.60),
    'UpperLumClamp':  (1.00, 1.30, 0.88, 1.02),
    'SunlightDimmer': (0.50, 2.00, 0.90, 2.70),
    'BrightScale':    (1.00, 3.00, 2.50, 4.00),
}

# Bloom Blur Radius is 7.0 in ALL 213 vanilla weather-used imagespaces — it is
# an engine constant, not authored data.  TES4's BlurRadius belongs to
# Oblivion's own blur pass (a different quantity that happens to share a name)
# and ranges 4..8; feeding it through made the bloom kernel too tight.
_IMGS_BLOOM_BLUR_RADIUS = 7.0


def _rescale(name: str, value: float, default: float) -> float:
    """Map a TES4 HDR value from its own range onto the vanilla TES5 range."""
    span = _IMGS_FIELD_RESCALE.get(name)
    if span is None:
        return value
    lo_in, hi_in, lo_out, hi_out = span
    if hi_in <= lo_in:
        return default
    t = (value - lo_in) / (hi_in - lo_in)
    t = min(max(t, 0.0), 1.0)
    return lo_out + t * (hi_out - lo_out)


def _sky_luminance(rec: dict, time: int) -> float:
    """Rec.709 luminance of the weather's Sky-Upper colour at a time of day."""
    raw = get_hex_bytes(rec, 'NAM0.Data')
    if not raw or len(raw) < 160:
        return _IMGS_SKY_LUM_DARK
    o = (_T4_SKY_UPPER * 4 + time) * 4
    return 0.299 * raw[o] + 0.587 * raw[o + 1] + 0.114 * raw[o + 2]


def _wthr_imgs(rec: dict, imgs_fid: int, time: int) -> bytes:
    """Build one time-of-day IMGS carrying this weather's HDR tone mapping."""
    # A TES4 weather whose whole HNAM block is zero has no authored HDR at all
    # (Oblivion's CS supplied the defaults, so the record was never filled in).
    # DefaultWeather is one, and it is the weather the 57 CNAM-less
    # worldspaces fall back to.  Clamping those zeros to the range MINIMUM
    # pins every field at its darkest/flattest legal value; fall back to the
    # vanilla per-slot defaults instead, which is what Oblivion itself did.
    authored = any(
        get_float(rec, 'HNAM.' + f, 0.0) != 0.0
        for f in ('EyeAdaptSpeed', 'BlurRadius', 'TargetLum', 'UpperLumClamp',
                  'BrightScale', 'BrightClamp', 'SunlightDimmer'))

    def h(field, default=0.0):
        if not authored:
            return default
        return _rescale(field, get_float(rec, 'HNAM.' + field, default), default)

    # Oblivion 0..1 adaptation rate -> Skyrim's scale, then biased per slot.
    speed = max(0.0, min(1.0, h('EyeAdaptSpeed', _TES4_EYE_ADAPT_DEFAULT)))
    lo, hi = _IMGS_HNAM_RANGES[0]
    eye_adapt = (lo + speed * (hi - lo)) * _IMGS_SLOT_EYE_ADAPT_BIAS[time]

    # Bloom threshold: blend TES4's BrightClamp toward the vanilla slot median.
    w = _IMGS_BLOOM_TES4_WEIGHT
    bloom_threshold = (w * h('BrightClamp', _IMGS_SLOT_BLOOM_THRESHOLD[time])
                       + (1.0 - w) * _IMGS_SLOT_BLOOM_THRESHOLD[time])

    # Sky Scale: ramp from the dark-sky value to the lit-sky value across the
    # luminance band where vanilla transitions, rather than stepping, so a
    # dim-but-not-black dawn sky does not jump to the full daytime value.
    lum = _sky_luminance(rec, time)
    t = min(max((lum - _IMGS_SKY_LUM_DARK) / _IMGS_SKY_LUM_DARK, 0.0), 1.0)
    sky_scale = (_IMGS_SKY_SCALE_DARK
                 + t * (_IMGS_SKY_SCALE_LIT - _IMGS_SKY_SCALE_DARK))

    # Defaults are the vanilla weather-used medians, so a weather with no
    # authored TES4 HDR renders like a normal Skyrim exterior.
    values = [
        eye_adapt,
        _IMGS_BLOOM_BLUR_RADIUS,     # Bloom Blur Radius (constant in vanilla)
        bloom_threshold,             # Bloom Threshold
        h('BrightScale', 3.0),       # Bloom Scale
        h('TargetLum', 0.575),       # Receive Bloom Threshold
        h('UpperLumClamp', 0.95),    # White
        h('SunlightDimmer', 1.9) * _IMGS_SLOT_SUNLIGHT_BIAS[time],
        sky_scale,
        _IMGS_SLOT_EYE_ADAPT_STRENGTH[time],
    ]
    values = [min(max(v, lo), hi)
              for v, (lo, hi) in zip(values, _IMGS_HNAM_RANGES)]
    hnam = struct.pack('<9f', *values)

    edid = get_str(rec, 'EditorID') or f"WTHR{get_formid(rec, 'FormID'):08X}"
    subs = pack_string_subrecord(
        'EDID', f'TES4_{edid}_IMGS{_IMGS_SLOT_NAMES[time]}')
    subs += pack_subrecord('HNAM', hnam)
    # Neutral cinematic/tint: TES4 has no equivalent, and every vanilla IMGS
    # with an HNAM also ships CNAM+TNAM.
    subs += pack_subrecord('CNAM', struct.pack('<3f', 1.0, 1.0, 1.0))
    subs += pack_subrecord('TNAM', struct.pack('<4f', 0.0, 1.0, 1.0, 1.0))
    return pack_record('IMGS', imgs_fid, 0, subs)

# EditorIDs Oblivion and Skyrim both use.  Ours is remapped into index 01 and
# would otherwise make the CK rename it to '<name>DUPLICATE001'.
_WTHR_EDID_COLLISIONS = frozenset({'DefaultWeather'})

# TES4 DATA 'Flags' classification bits.  TES5 defines the same four in the
# same positions and adds two aurora bits TES4 has no source for, so the low
# nibble passes straight through.
_WTHR_CLASSIFICATION_MASK = 0x0F


def _wthr_flags(rec: dict) -> int:
    """TES5 DATA weather-classification flags from the TES4 classification byte.

    Bits 0-3 (Pleasant/Cloudy/Rainy/Snow) are shared; bits 4-5 are TES5's
    aurora controls.  Weather with no classification at all is treated as
    Pleasant so the engine's weather-transition picker can still match it.
    """
    flags = get_int(rec, 'DATA.Classification') & _WTHR_CLASSIFICATION_MASK
    if flags == 0:
        flags = 0x01  # Weather - Pleasant
    return flags


def _wthr_cloud_sig(layer: int) -> bytes:
    """Build the 4-byte cloud texture signature for a given layer index (0-28).

    Layer 0-16:  first byte is chr(0x30 + layer), rest is '0TX'
                 e.g. layer 0 = '00TX', layer 1 = '10TX', layer 10 = ':0TX'
    Layer 17-28: first byte is chr(0x41 + (layer - 17)), rest is '0TX'
                 e.g. layer 17 = 'A0TX', layer 18 = 'B0TX'
    """
    if layer <= 16:
        return bytes([0x30 + layer]) + b'0TX'
    else:
        return bytes([0x41 + (layer - 17)]) + b'0TX'


# --- NAM0 weather colour tables -------------------------------------------
#
# TES4 NAM0 is 10 colour types x 4 times-of-day x RGBA = 160 bytes.
# TES5 NAM0 is 17 colour types x 4 times-of-day x RGBA = 272 bytes (verified
# against references/Skyrim.esm: 72 of 84 vanilla WTHR use the full 272; the
# short 224/208 variants are older form versions).
#
# Times-of-day are in the same order in both games (Sunrise, Day, Sunset,
# Night), so only the TYPE axis needs remapping.  Index = TES5 slot, value =
# the TES4 slot it is sourced from, or None for a TES5-only slot.
#
# TES4 order (wbDefinitionsTES4 wbWeatherColors):
#   0 Sky-Upper, 1 Fog, 2 Clouds-Lower, 3 Ambient, 4 Sunlight, 5 Sun,
#   6 Stars, 7 Sky-Lower, 8 Horizon, 9 Clouds-Upper
_T4_SKY_UPPER, _T4_FOG, _T4_CLOUDS_LOWER, _T4_AMBIENT = 0, 1, 2, 3
_T4_SUNLIGHT, _T4_SUN, _T4_STARS, _T4_SKY_LOWER = 4, 5, 6, 7
_T4_HORIZON, _T4_CLOUDS_UPPER = 8, 9

# TES5 order (wbDefinitionsCommon wbWeatherColors, gmTES5 branch):
#   0 Sky-Upper, 1 Fog Near, 2 Unused, 3 Ambient, 4 Sunlight, 5 Sun, 6 Stars,
#   7 Sky-Lower, 8 Horizon, 9 Effect Lighting, 10 Cloud LOD Diffuse,
#   11 Cloud LOD Ambient, 12 Fog Far, 13 Sky Statics, 14 Water Multiplier,
#   15 Sun Glare, 16 Moon Glare
_NAM0_TES5_FROM_TES4 = [
    _T4_SKY_UPPER,      # 0  Sky-Upper
    _T4_FOG,            # 1  Fog Near
    None,               # 2  Unused (TES4 had Clouds-Lower here)
    _T4_AMBIENT,        # 3  Ambient
    _T4_SUNLIGHT,       # 4  Sunlight
    _T4_SUN,            # 5  Sun
    _T4_STARS,          # 6  Stars
    _T4_SKY_LOWER,      # 7  Sky-Lower
    _T4_HORIZON,        # 8  Horizon
    None,               # 9  Effect Lighting — no TES4 source
    _T4_CLOUDS_UPPER,   # 10 Cloud LOD Diffuse  <- TES4 upper cloud tint
    _T4_CLOUDS_LOWER,   # 11 Cloud LOD Ambient  <- TES4 lower cloud tint
    _T4_FOG,            # 12 Fog Far — TES4 had a single fog colour
    None,               # 13 Sky Statics — see _NAM0_SLOT_DEFAULTS
    None,               # 14 Water Multiplier
    None,               # 15 Sun Glare
    None,               # 16 Moon Glare
]

# Documented per-slot defaults for the TES5-only slots (UESP
# 'Skyrim Mod:Mod File Format/WTHR', corroborated by a census of the 84 vanilla
# Skyrim.esm WTHR records).
#
# These are NOT free to guess: slots 13/15/16 tint additive sky passes, so a
# wrong value blows the scene out rather than merely looking off.
#   13 Sky Statics    — UESP "defaults to black"; vanilla mode (0,0,0).
#                       This tints the CLMT stars/moon mesh; WHITE HERE MAKES
#                       THE NIGHT SKY BLINDING.
#   14 Water Multiplier — UESP "defaults to white"; vanilla mode (255,255,255).
#                       Multiplies water reflection, so black would flatten it.
#   15 Sun Glare      — UESP says defaults to white, but 35 of 84 vanilla
#                       records ship BLACK and the rest are dark browns
#                       (e.g. SkyrimClear = 35,21,7). Copying the TES4 Sun
#                       colour here produced a blazing glare.
#   16 Moon Glare     — same story; vanilla mode is black.
#
# TES4 has no source colour for any of them, so use the vanilla-mode value.
_NAM0_SLOT_DEFAULTS = {
    13: (0, 0, 0),
    14: (255, 255, 255),
    15: (0, 0, 0),
    16: (0, 0, 0),
}

_TES5_NAM0_SLOTS = 17
_TES5_CLOUD_LAYERS = 32

# DALC face brightness relative to NAM0's Ambient colour, in xEdit's field
# order (X+, X-, Y+, Y-, Z+, Z-).  Medians measured over all 84 vanilla
# Skyrim.esm weather records; see _wthr_dalc.
_DALC_FACE_WEIGHTS = (0.98, 0.94, 0.96, 0.95, 0.67, 1.28)


def _wthr_nam0(rec: dict) -> bytes:
    """Remap the TES4 160-byte weather colour table into TES5's 272-byte one.

    Returns 272 bytes: 17 colour types x 4 times-of-day x RGBA.
    """
    raw = get_hex_bytes(rec, 'NAM0.Data')
    out = bytearray(_TES5_NAM0_SLOTS * 4 * 4)

    for slot, (r, g, b) in _NAM0_SLOT_DEFAULTS.items():
        for time in range(4):
            off = (slot * 4 + time) * 4
            out[off:off + 4] = bytes((r, g, b, 0))

    if not raw or len(raw) < 160:
        return bytes(out)

    for t5_slot, t4_slot in enumerate(_NAM0_TES5_FROM_TES4):
        if t4_slot is None:
            continue
        for time in range(4):
            src = (t4_slot * 4 + time) * 4
            dst = (t5_slot * 4 + time) * 4
            # TES4 stores RGBA with the 4th byte unused; TES5 is identical.
            out[dst:dst + 3] = raw[src:src + 3]
    return bytes(out)


def _cloud_speed_tes4_to_tes5(speed: int) -> int:
    """Convert a TES4 cloud-speed byte to TES5's RNAM/QNAM encoding.

    BOTH GAMES USE THE SAME PHYSICAL SCALE, so this is a real unit conversion
    rather than a passthrough:

    * TES4 stores an UNSIGNED 0..255 byte scaled against the engine setting
      `fWeatherCloudSpeedMax`, whose default is 0.1 — read straight out of
      Oblivion.exe, where the settings constructor at 0x9E5BF0 does
      `fld dword ptr [0xA2FAAC]` (= 0.1) before pushing the name string at
      0xA56C88.  So byte b means b/255 * 0.1, always forwards.
    * TES5 stores a SIGNED drift over the same -0.1..+0.1 range, encoded
      0x00 = -0.1, 0x7F = 0.0, 0xFE = +0.1 (UESP 'Skyrim Mod:Mod File
      Format/WTHR'; xEdit's wbWeatherCloudSpeedToStr computes
      (value-127)/127/10, and wbWeatherCloudSpeedToInt clamps to 254).

    Mapping TES4's 0..0.1 onto the positive half gives 0x7F..0xFE.  The old
    `0x7F + speed//2` treated the byte as if it were already TES5-encoded and
    ran clouds at up to ~10x their intended speed.
    """
    speed = max(0, min(255, speed))
    return min(254, 127 + round(speed / 255.0 * 127.0))


def _wthr_cloud_arrays(rec: dict, used_layers) -> bytes:
    """Build TES5's per-cloud-layer RNAM/QNAM/PNAM/JNAM arrays.

    TES4 has no per-layer data — it has exactly two cloud layers and a single
    speed byte for each.  Layers 0/1 therefore take the TES4 lower/upper cloud
    speed and colour; every other layer gets the vanilla neutral value.

    Sizes verified against references/Skyrim.esm (83/84 vanilla records):
      RNAM 32B  — cloud speed Y, u8 per layer, 0x7F = neutral (no drift)
      QNAM 32B  — cloud speed X, u8 per layer, 0x7F = neutral
      PNAM 512B — cloud colours, 32 layers x 4 times x RGBA
      JNAM 512B — cloud alphas, 32 layers x 4 times x f32
    """
    speed_lower = get_int(rec, 'DATA.CloudSpeedLower')
    speed_upper = get_int(rec, 'DATA.CloudSpeedUpper')

    rnam = bytearray(b'\x7F' * _TES5_CLOUD_LAYERS)
    qnam = bytearray(b'\x7F' * _TES5_CLOUD_LAYERS)
    rnam[0] = _cloud_speed_tes4_to_tes5(speed_lower)
    rnam[1] = _cloud_speed_tes4_to_tes5(speed_upper)

    # Cloud tints come from the TES4 colour table's Clouds-Lower/Clouds-Upper
    # rows so the layers match the sky they are drawn against.
    raw = get_hex_bytes(rec, 'NAM0.Data')
    pnam = bytearray(_TES5_CLOUD_LAYERS * 4 * 4)
    if raw and len(raw) >= 160:
        for layer, t4_slot in ((0, _T4_CLOUDS_LOWER), (1, _T4_CLOUDS_UPPER)):
            for time in range(4):
                src = (t4_slot * 4 + time) * 4
                dst = (layer * 4 + time) * 4
                pnam[dst:dst + 3] = raw[src:src + 3]

    # Cloud alpha, per layer per time-of-day.  Only the layers that actually
    # carry a texture may be opaque: a blanket 1.0 across all 32 layers asks
    # the engine to draw 30 fully-opaque empty layers on top of the sky.
    # TES4 has no per-layer alpha curve, so the two real layers are opaque and
    # the rest are transparent.
    alphas = [0.0] * (_TES5_CLOUD_LAYERS * 4)
    for layer in used_layers:
        for time in range(4):
            alphas[layer * 4 + time] = 1.0
    jnam = struct.pack('<%df' % (_TES5_CLOUD_LAYERS * 4), *alphas)

    return (pack_subrecord('RNAM', bytes(rnam))
            + pack_subrecord('QNAM', bytes(qnam))
            + pack_subrecord('PNAM', bytes(pnam))
            + pack_subrecord('JNAM', jnam))


def _wthr_dalc(rec: dict) -> bytes:
    """Build the four DALC directional-ambient blocks (sunrise/day/sunset/night).

    TES5 lights the world with a 6-direction ambient cube (X+/X-/Y+/Y-/Z+/Z-)
    that TES4 has no equivalent for, so it is derived from the TES4 Ambient
    colour for the same time of day.

    The per-face WEIGHTS are measured from the 84 vanilla Skyrim.esm weathers
    (median of face/NAM0-Ambient over every record, time and channel):

        X+ 0.98   X- 0.94   Y+ 0.96   Y- 0.95   Z+ 0.67   Z- 1.28

    Z+ is the DARKEST face and Z- the brightest — the opposite of the
    intuition that the sky-facing side should be brighter.  Writing Ambient
    verbatim into all six faces and then brightening Z+ (the previous
    behaviour) overdrove every face and washed the scene out.

    Layout (wbAmbientColors, form version >= 34): 6 x RGBA + Specular RGBA +
    Fresnel Power f32 = 32 bytes.  Fresnel is 1.0 in every vanilla record.
    """
    raw = get_hex_bytes(rec, 'NAM0.Data')
    out = b''
    for time in range(4):
        if raw and len(raw) >= 160:
            src = (_T4_AMBIENT * 4 + time) * 4
            r, g, b = raw[src], raw[src + 1], raw[src + 2]
        else:
            r = g = b = 0
        block = bytearray()
        for weight in _DALC_FACE_WEIGHTS:
            block += bytes((min(255, round(r * weight)),
                            min(255, round(g * weight)),
                            min(255, round(b * weight)), 0))
        block += b'\x00\x00\x00\x00'          # Specular
        block += struct.pack('<f', 1.0)       # Fresnel Power
        out += pack_subrecord('DALC', bytes(block))
    return out


def convert_WTHR(rec: dict, writer=None) -> tuple:
    """WTHR — Weather conversion.  Returns (wthr_bytes, [imgs_bytes, ...]).

    TES5 subrecord order (from wbDefinitionsTES5.pas):
    EDID, DNAM/CNAM/ANAM/BNAM (old, unused), cloud textures (00TX..O0TX),
    LNAM, MNAM, NNAM, ONAM(unused), RNAM, QNAM, PNAM, JNAM, NAM0, FNAM,
    DATA, NAM1, SNAM(sounds), TNAM(sky statics), IMSP, HNAM(SSE volumetric),
    DALC x4, NAM2, NAM3, MODL/MODT(aurora), GNAM
    """
    # HDR tone mapping lives in companion IMGS records in TES5 — see
    # _wthr_imgs.  FOUR of them, one per time of day, because day and night
    # tone mapping differ substantially and 70% of vanilla weathers ship
    # distinct imagespaces per slot.  Every weather gets them, including those
    # whose TES4 HNAM is all zeros (DefaultWeather): the clamp in _wthr_imgs
    # turns those into the vanilla floor rather than a degenerate zero white
    # point.  A caller with no writer (unit tests exercising the WTHR bytes
    # alone) falls back to stock.
    imgs_bytes = []
    imgs_fids = [_DEFAULT_IMGS] * 4
    if writer is not None:
        imgs_fids = []
        for time in range(4):
            fid = writer.alloc_formid()
            imgs_fids.append(fid)
            imgs_bytes.append(_wthr_imgs(rec, fid, time))

    subs = b''
    edid = get_str(rec, 'EditorID')
    if edid:
        # Oblivion and Skyrim both ship a 'DefaultWeather'.  After load-order
        # remapping ours collides with Skyrim's 0x0000015E and the CK renames
        # it to 'DefaultWeatherDUPLICATE001'; prefix instead so the converted
        # record keeps a stable, meaningful EditorID.
        if edid in _WTHR_EDID_COLLISIONS:
            edid = 'TES4' + edid
        subs += pack_string_subrecord('EDID', edid)

    # Cloud layer textures — TES4's two layers become TES5 layers 0 and 1.
    lower_cloud = get_str(rec, 'CNAM.LowerCloudLayer')
    upper_cloud = get_str(rec, 'DNAM.UpperCloudLayer')
    used_layers = []
    for layer, path in ((0, lower_cloud), (1, upper_cloud)):
        if not path:
            continue
        sig = _wthr_cloud_sig(layer)
        path_bytes = _prefix_path(path).encode('utf-8') + b'\x00'
        subs += sig + struct.pack('<H', len(path_bytes)) + path_bytes
        used_layers.append(layer)

    # LNAM — Max Cloud Layers.  Vanilla is always 29 (0x1D); xEdit marks the
    # field required with that default.  0 made the engine allocate no layers.
    subs += pack_uint32_subrecord('LNAM', 29)

    # MNAM (Precipitation Type -> SPGD) and NNAM (Visual Effect -> RFCT) are
    # .SetRequired in xEdit.  TES4 drives precipitation from the weather's own
    # particle textures and has no record to map here, so emit the explicit
    # NULL that vanilla records use rather than omitting the subrecord.
    subs += pack_formid_subrecord('MNAM', 0)
    subs += pack_formid_subrecord('NNAM', 0)

    # RNAM/QNAM/PNAM/JNAM — per-cloud-layer speed, colour and alpha.
    subs += _wthr_cloud_arrays(rec, used_layers)

    # NAM0 — weather colours, remapped from TES4's 10 types to TES5's 17.
    subs += pack_subrecord('NAM0', _wthr_nam0(rec))

    # FNAM — Fog distances (TES5: 32 bytes — 8 floats)
    fog_day_near = get_float(rec, 'FNAM.FogDayNear', 100.0)
    fog_day_far = get_float(rec, 'FNAM.FogDayFar', 100000.0)
    fog_night_near = get_float(rec, 'FNAM.FogNightNear', 100.0)
    fog_night_far = get_float(rec, 'FNAM.FogNightFar', 100000.0)
    fnam = struct.pack('<ffffffff',
                        fog_day_near, fog_day_far,
                        fog_night_near, fog_night_far,
                        1.0, 1.0,    # Day/Night power
                        1.0, 1.0)    # Day/Night max
    subs += pack_subrecord('FNAM', fnam)

    # DATA — Weather Data (19 bytes in TES5).
    #
    # TES5 reuses TES4's field order but replaces TES4's two cloud-speed bytes
    # (offsets 1-2) with padding, having moved per-layer speed into RNAM/QNAM,
    # and appends four fields TES4 has no source for.
    data = struct.pack(
        '<B2xBBBBBBBBBBBBBBBB',
        get_int(rec, 'DATA.WindSpeed'),
        get_int(rec, 'DATA.TransDelta'),
        get_int(rec, 'DATA.SunGlare'),
        get_int(rec, 'DATA.SunDamage'),
        get_int(rec, 'DATA.PrecipBeginFadeIn'),
        get_int(rec, 'DATA.PrecipEndFadeOut'),
        get_int(rec, 'DATA.ThunderBeginFadeIn'),
        get_int(rec, 'DATA.ThunderEndFadeOut'),
        get_int(rec, 'DATA.ThunderFrequency'),
        _wthr_flags(rec),
        get_int(rec, 'DATA.LightningR'),
        get_int(rec, 'DATA.LightningG'),
        get_int(rec, 'DATA.LightningB'),
        0,    # Visual Effect - Begin (no TES4 source)
        0,    # Visual Effect - End
        0,    # Wind Direction
        0,    # Wind Direction Range
    )
    subs += pack_subrecord('DATA', data)

    # NAM1 — Disabled Cloud Layers bitfield.  This must disable only the
    # layers we did NOT write a texture for; the old blanket 0xFFFFFFFF also
    # disabled layers 0/1 and blanked every converted sky.
    disabled = 0xFFFFFFFF
    for layer in used_layers:
        disabled &= ~(1 << layer)
    subs += pack_uint32_subrecord('NAM1', disabled & 0xFFFFFFFF)

    # Sounds — SNAM (after NAM1 per xEdit)
    sc = get_int(rec, 'SoundCount')
    for i in range(sc):
        sfid = get_formid(rec, f'Sound[{i}].FormID')
        stype = get_int(rec, f'Sound[{i}].Type')
        if sfid:
            subs += pack_subrecord('SNAM', struct.pack('<II', sfid, stype))

    # IMSP — Image Spaces (sunrise/day/sunset/night), each pointing at the
    # matching time-of-day IMGS built from this weather's TES4 HDR block.
    subs += pack_subrecord('IMSP', struct.pack('<IIII', *imgs_fids))

    # DALC — Directional Ambient Lighting Colors (4 x 32 bytes)
    subs += _wthr_dalc(rec)

    wthr_bytes = pack_record('WTHR', get_formid(rec, 'FormID'),
                             get_int(rec, 'RecordFlags'), subs)
    return wthr_bytes, imgs_bytes


# Skyrim's night-sky mesh.  TES4 climates point MODL at their own stars mesh
# (Sky\Stars.nif), which the asset pipeline converts under the tes4\ namespace;
# this is only the fallback for a climate that authored no model at all.
_DEFAULT_STARS_MODEL = 'Sky\\Stars.nif'


def convert_CLMT(rec: dict) -> bytes:
    """CLMT — Climate.

    TES4 and TES5 climates are near-identical: the same WLST weather list, the
    same FNAM/GNAM sun textures, the same 6-byte TNAM timing struct.  The only
    format change is that TES5's WLST entry gained a trailing Global FormID,
    widening it from 8 to 12 bytes (verified against references/Skyrim.esm,
    whose entries are all 12 bytes with a null Global).

    Without this record the converted WTHR records are unreachable: weather is
    selected through WRLD -> CNAM -> CLMT -> WLST, never referenced directly.

    Subrecord order (wbDefinitionsTES5.pas:4444):
    EDID, WLST, FNAM, GNAM, MODL/MODT, TNAM
    """
    subs = b''
    edid = get_str(rec, 'EditorID')
    if edid:
        subs += pack_string_subrecord('EDID', edid)

    # WLST — weather list.  TES5 entry is (Weather, Chance, Global).
    wlst = b''
    for i in range(get_int(rec, 'WeatherCount')):
        wfid = get_formid(rec, f'Weather[{i}].FormID')
        if not wfid:
            continue
        chance = get_int(rec, f'Weather[{i}].Chance')
        wlst += struct.pack('<IiI', wfid, chance, 0)
    if wlst:
        subs += pack_subrecord('WLST', wlst)

    # Sun and sun-glare textures.
    sun = get_str(rec, 'FNAM.SunTexture')
    if sun:
        subs += pack_string_subrecord('FNAM', _prefix_path(sun))
    glare = get_str(rec, 'GNAM.GlareTexture')
    if glare:
        subs += pack_string_subrecord('GNAM', _prefix_path(glare))

    # MODL — the night-sky/stars mesh.  Every vanilla Skyrim climate has one;
    # without it the engine draws no stars at night.
    model = get_str(rec, 'Model.MODL') or _DEFAULT_STARS_MODEL
    subs += pack_string_subrecord('MODL', _prefix_path(model))
    # MODT stub (version 2, no texture hashes) — same form the GRAS converter
    # uses; vanilla climates ship a 12-byte MODT.
    subs += pack_subrecord('MODT', struct.pack('<III', 2, 0, 0))

    # TNAM — 6-byte timing struct, identical in both games:
    # sunrise begin/end, sunset begin/end (units of 10 minutes), volatility,
    # and a packed moons/phase-length byte.
    subs += pack_subrecord('TNAM', bytes((
        get_int(rec, 'TNAM.SunriseBegin'),
        get_int(rec, 'TNAM.SunriseEnd'),
        get_int(rec, 'TNAM.SunsetBegin'),
        get_int(rec, 'TNAM.SunsetEnd'),
        get_int(rec, 'TNAM.Volatility'),
        get_int(rec, 'TNAM.MoonsPhaseLength'),
    )))

    return pack_record('CLMT', get_formid(rec, 'FormID'), get_int(rec, 'RecordFlags'), subs)
