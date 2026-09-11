// Small things the three Direct3D translation units all need.
//
// Split out rather than duplicated: `Constants` in particular has to match
// shaders/common.hlsli byte for byte, and two copies of a struct that must
// match a third file is a drift waiting to happen.

#pragma once

#include <cstdint>

// Must match the cbuffer in shaders/common.hlsli exactly, including the tail
// padding: a constant buffer is a whole number of 16-byte registers, and a
// short one is rejected outright.
struct Constants
{
    uint32_t easu0[4];
    uint32_t easu1[4];
    uint32_t easu2[4];
    uint32_t easu3[4];
    uint32_t rcas[4];
    int32_t src_offset[2];
    int32_t src_size[2];
    int32_t dst_offset[2];
    int32_t dst_size[2];
    int32_t src_tex_size[2];
    uint32_t color_matrix;
    uint32_t color_full_range;
    uint32_t backdrop;
    uint32_t pad0;
    uint32_t pad1[2];
};
static_assert(sizeof(Constants) == 144, "constant buffer must match common.hlsli");

struct OverlayConstants
{
    float rect[4];
};

// AVColorSpace -> the shader's matrix index.
//
// Unspecified is the common case off a capture card, and the convention every
// player follows is to pick by resolution: standard definition was BT.601 and
// high definition is BT.709. Guessing wrong is not subtle -- skin tones go
// green or magenta -- but it is also not something anyone can report usefully,
// which is why the value travels from the decoded frame at all.
inline uint32_t MatrixFor(int32_t colorspace, int32_t height)
{
    switch (colorspace)
    {
    case 5:   // AVCOL_SPC_BT470BG
    case 6:   // AVCOL_SPC_SMPTE170M
        return 0;  // BT.601
    case 9:   // AVCOL_SPC_BT2020_NCL
    case 10:  // AVCOL_SPC_BT2020_CL
        return 2;  // BT.2020
    case 1:   // AVCOL_SPC_BT709
        return 1;
    default:
        return height < 720 ? 0u : 1u;
    }
}

// AVColorRange: 2 is JPEG/full, everything else is limited. Limited is the
// right default -- it is what essentially every console and capture card
// produces, and treating limited video as full crushes blacks and clips
// highlights in a way that reads as "the stream looks contrasty".
inline uint32_t FullRangeFor(int32_t color_range)
{
    return color_range == 2 ? 1u : 0u;
}

inline uint32_t DivRoundUp(uint32_t value, uint32_t divisor)
{
    return (value + divisor - 1) / divisor;
}
