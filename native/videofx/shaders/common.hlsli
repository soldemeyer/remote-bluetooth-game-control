// Shared declarations for every rbgc_videofx compute shader.
//
// One constant buffer for all of them. The passes run back to back on the same
// frame and share almost every value, so separate buffers would mean several
// map/unmap pairs per frame for no benefit -- and this sits on a thread with a
// sub-millisecond budget.
//
// Everything is in PERCEPTUAL (gamma) space and stays there. FSR 1 is specified
// against tonemapped, non-linear input, and decoded H.264 already is that. A
// linearise/de-linearise pair around EASU would be two extra transcendentals
// per pixel and a picture that no longer matches what the console sent.

#ifndef RBGC_COMMON_HLSLI
#define RBGC_COMMON_HLSLI

cbuffer Constants : register(b0)
{
    // FSR 1's own packed constants, straight out of FsrEasuCon/FsrRcasCon.
    uint4 EasuCon0;
    uint4 EasuCon1;
    uint4 EasuCon2;
    uint4 EasuCon3;
    uint4 RcasCon;

    // Where in the source texture this piece starts, and how big it is, in
    // texels. The crop: the upscaler never sees a pixel outside it.
    int2  SrcOffset;
    int2  SrcSize;

    // Where the result lands in the destination, in texels.
    int2  DstOffset;
    int2  DstSize;

    // Size of the texture SrcOffset/SrcSize index into, for normalising.
    int2  SrcTexSize;

    // BT.601 vs BT.709, limited vs full range. Read off the decoded frame
    // rather than assumed -- a hardcoded matrix makes the picture shift
    // colour when the mode changes, which gets reported as "FSR looks washed
    // out" rather than as a colour bug.
    uint  ColorMatrix;      // 0 = BT.601, 1 = BT.709, 2 = BT.2020
    uint  ColorFullRange;   // 0 = limited (16-235), 1 = full

    // 0xRRGGBB, the letterbox. Pushed rather than baked in, because the
    // client's theme is switchable while it is running.
    uint  Backdrop;
    uint  Padding0;
};

// -- colour ------------------------------------------------------------------

static const float3x3 kBT601 = float3x3(
    1.0,  0.0,       1.402,
    1.0, -0.344136, -0.714136,
    1.0,  1.772,     0.0);

static const float3x3 kBT709 = float3x3(
    1.0,  0.0,       1.5748,
    1.0, -0.187324, -0.468124,
    1.0,  1.8556,    0.0);

static const float3x3 kBT2020 = float3x3(
    1.0,  0.0,       1.4746,
    1.0, -0.16455312, -0.57135312,
    1.0,  1.8814,    0.0);

float3 YuvToRgb(float y, float u, float v)
{
    if (ColorFullRange == 0)
    {
        // Limited range: luma 16-235, chroma 16-240, both over 255.
        y = (y - 16.0 / 255.0) * (255.0 / 219.0);
        u = (u - 128.0 / 255.0) * (255.0 / 224.0);
        v = (v - 128.0 / 255.0) * (255.0 / 224.0);
    }
    else
    {
        u -= 128.0 / 255.0;
        v -= 128.0 / 255.0;
    }

    float3x3 m = kBT709;
    if (ColorMatrix == 0)      m = kBT601;
    else if (ColorMatrix == 2) m = kBT2020;

    return saturate(mul(m, float3(y, u, v)));
}

float3 BackdropRgb()
{
    return float3(
        float((Backdrop >> 16) & 0xFFu) / 255.0,
        float((Backdrop >> 8) & 0xFFu) / 255.0,
        float(Backdrop & 0xFFu) / 255.0);
}

// FSR 1's reference dispatch: 64 threads arranged as 8x8 via a Morton-ish
// remap, each handling a 2x2 quad, so one group covers 16x16 output pixels.
// Kept exactly as the reference uses it -- the remap is what gives the gather
// operations their cache locality, and "tidying" it into a plain 2D index
// measurably costs bandwidth.
#define RBGC_THREADS 64
#define RBGC_TILE 16

#endif // RBGC_COMMON_HLSLI
