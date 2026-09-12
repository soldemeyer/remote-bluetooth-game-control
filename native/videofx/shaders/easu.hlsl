// FSR 1 pass 1: EASU, Edge-Adaptive Spatial Upsampling.
//
// The algorithm is AMD's, unmodified, from third_party/ffx_fsr1.h. This file
// supplies only the three gather callbacks it requires and the reference
// dispatch. Nothing here reimplements, approximates or "simplifies" it.
//
// Input is perceptual (gamma) space and stays that way. FSR 1 is specified
// against tonemapped output, and decoded H.264 already is that.
//
// THE ONE ADDITION, and it is a correctness fix rather than a tweak
// -------------------------------------------------------------------
// The reference assumes its input viewport starts at texel (0,0). Here it
// often does not: a client showing two or three pieces of a split screen
// converts the whole union once and then upscales sub-rectangles of it, and
// those sub-rectangles are ADJACENT IN THE SOURCE -- they are neighbouring
// quadrants of the console's picture.
//
// EASU's kernel reaches +/-2 texels. With the sampler merely clamping to the
// texture edge, a piece at a shared boundary would gather from the piece next
// to it: a thin strip of another player's game, sharpened and scaled up along
// the seam. Small, but it is exactly the leak this whole feature exists to
// prevent, and it would look like an encoder artefact rather than a bug.
//
// So the gather coordinate is clamped to the piece's own rectangle, inset by
// the kernel radius. The outermost texels are replicated instead, which is
// invisible and cannot show a neighbour.

#define A_GPU 1
#define A_HLSL 1
#include "../third_party/ffx_a.h"

#include "common.hlsli"

Texture2D<float4> Source : register(t0);
SamplerState LinearClamp : register(s0);
RWTexture2D<float4> Output : register(u0);

// The piece's rectangle in normalised coordinates, inset by EASU's kernel
// radius so a 12-tap gather cannot reach outside it.
static const float kKernelRadius = 2.0;

float2 ClampToPiece(float2 p)
{
    float2 texel = 1.0 / float2(SrcTexSize);
    float2 lo = (float2(SrcOffset) + kKernelRadius) * texel;
    float2 hi = (float2(SrcOffset + SrcSize) - kKernelRadius) * texel;
    // A piece smaller than the kernel would invert the bounds; collapse to its
    // centre rather than producing a NaN and a black hole in the picture.
    hi = max(hi, lo);
    return clamp(p, lo, hi);
}

// The offset that turns a viewport-relative coordinate into a texture one.
float2 PieceOrigin()
{
    return float2(SrcOffset) / float2(SrcTexSize);
}

AF4 FsrEasuRF(AF2 p) { return Source.GatherRed(LinearClamp, ClampToPiece(p + PieceOrigin())); }
AF4 FsrEasuGF(AF2 p) { return Source.GatherGreen(LinearClamp, ClampToPiece(p + PieceOrigin())); }
AF4 FsrEasuBF(AF2 p) { return Source.GatherBlue(LinearClamp, ClampToPiece(p + PieceOrigin())); }

#define FSR_EASU_F 1
#include "../third_party/ffx_fsr1.h"

void Filter(AU2 pos)
{
    if (pos.x >= (AU1)DstSize.x || pos.y >= (AU1)DstSize.y)
        return;

    AF3 colour;
    FsrEasuF(colour, pos, EasuCon0, EasuCon1, EasuCon2, EasuCon3);
    Output[int2(pos)] = float4(colour, 1.0);
}

// The reference dispatch: 64 threads remapped to an 8x8 pattern, each writing
// a 2x2 quad, so one group covers 16x16 output pixels. `ARmp8x8` is what gives
// the gathers their locality -- replacing it with a plain 2D thread index
// measurably costs bandwidth, so it stays as AMD wrote it.
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
