// FSR 1 pass 2: RCAS, Robust Contrast-Adaptive Sharpening.
//
// AMD's algorithm, unmodified, from third_party/ffx_fsr1.h. This file supplies
// the two callbacks it requires, the reference dispatch, and the destination
// offset that puts the result where the compositor wants it.
//
// FSR_RCAS_DENOISE is ON, and that is a deliberate choice for this
// application rather than a default. RCAS was designed to sharpen the output
// of a renderer; here it sharpens the output of a video decoder, which carries
// block and ringing artefacts the renderer case does not have. The denoise
// path is exactly the guard against amplifying them, and the cost is a few
// ALU operations on a pass that is bandwidth-bound anyway.
//
// Sharpness arrives as FidelityFX's own constant, computed on the CPU by
// FsrRcasCon. Note what that function does with it: `sharpness =
// exp2(-sharpness)`, i.e. the value is STOPS OF HALVING, where 0 is maximum
// sharpness. The client's slider is inverted and mapped onto that, and the raw
// number is never shown to anyone.
//
// This pass writes straight into the swapchain's back buffer at an offset,
// rather than into an intermediate the compositor then copies. One fewer
// full-resolution round trip through memory per piece per frame.

#define A_GPU 1
#define A_HLSL 1
#include "../third_party/ffx_a.h"

#include "common.hlsli"

Texture2D<float4> Source : register(t0);
RWTexture2D<float4> Output : register(u0);

// RCAS reads a 3x3 neighbourhood by integer position. Clamped to the source,
// because the tap outside the edge is otherwise whatever the allocation
// happened to contain -- and on a piece of a split screen it would be the
// neighbouring player's picture. Same reasoning as the gather clamp in
// easu.hlsl; here the radius is 1.
AF4 FsrRcasLoadF(ASU2 p)
{
    int2 clamped = clamp(int2(p), int2(0, 0), SrcSize - 1);
    return Source.Load(int3(clamped, 0));
}

// A hook for callers whose input is in a space RCAS must be told about. Ours
// is already perceptual, which is what RCAS wants, so there is nothing to do.
void FsrRcasInputF(inout AF1 r, inout AF1 g, inout AF1 b) {}

#define FSR_RCAS_F 1
#define FSR_RCAS_DENOISE 1
#include "../third_party/ffx_fsr1.h"

void Filter(AU2 pos)
{
    if (pos.x >= (AU1)DstSize.x || pos.y >= (AU1)DstSize.y)
        return;

    AF3 colour;
    FsrRcasF(colour.r, colour.g, colour.b, pos, RcasCon);
    Output[int2(pos) + DstOffset] = float4(colour, 1.0);
}

[numthreads(RBGC_THREADS, 1, 1)]
void main(uint3 localId : SV_GroupThreadID, uint3 groupId : SV_GroupID)
{
    AU2 gxy = ARmp8x8(localId.x) + AU2(groupId.x << 4u, groupId.y << 4u);
    Filter(gxy);
    gxy.x += 8u;
    Filter(gxy);
    gxy.y += 8u;
    Filter(gxy);
    gxy.x -= 8u;
    Filter(gxy);
}
