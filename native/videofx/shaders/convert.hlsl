// YUV -> RGBA, and the crop, in one pass.
//
// The software-decode entry point. Three planes arrive as R8 textures already
// pointing at the uploaded rectangle's origin, and this writes the RGBA the
// scalers sample from.
//
// The crop is not done here by sampling an offset -- it was done before the
// upload, by advancing the plane pointers. This pass converts exactly the
// pixels it was given and nothing else, which is what makes "crop before you
// upscale" true by construction rather than by a rectangle somebody has to
// keep correct.
//
// Chroma is sampled with a linear sampler at half resolution, which is the
// ordinary 4:2:0 reconstruction. Anything better belongs in the scaler that
// follows, not here.

#include "common.hlsli"

Texture2D<float> PlaneY : register(t0);
Texture2D<float> PlaneU : register(t1);
Texture2D<float> PlaneV : register(t2);
SamplerState     LinearClamp : register(s0);

RWTexture2D<float4> Output : register(u0);

[numthreads(8, 8, 1)]
void main(uint3 tid : SV_DispatchThreadID)
{
    int2 p = int2(tid.xy);
    if (p.x >= SrcSize.x || p.y >= SrcSize.y)
        return;

    // Luma is a texel fetch: one sample per output pixel, no filtering wanted.
    float y = PlaneY.Load(int3(p, 0));

    // Chroma is half resolution in both axes. +0.5 puts the sample at the
    // texel centre; without it every frame is shifted a quarter of a chroma
    // sample and the picture has a faint colour fringe on high-contrast edges.
    float2 uv = (float2(p) + 0.5) / float2(SrcSize) ;
    float u = PlaneU.SampleLevel(LinearClamp, uv, 0);
    float v = PlaneV.SampleLevel(LinearClamp, uv, 0);

    Output[p] = float4(YuvToRgb(y, u, v), 1.0);
}


// NV12 variant: luma in one plane, chroma interleaved in a second.
//
// Used for a hardware frame that has already been through the video
// processor, and for any decoder that hands back NV12 in system memory.
Texture2D<float>  NvPlaneY  : register(t0);
Texture2D<float2> NvPlaneUV : register(t1);

[numthreads(8, 8, 1)]
void main_nv12(uint3 tid : SV_DispatchThreadID)
{
    int2 p = int2(tid.xy);
    if (p.x >= SrcSize.x || p.y >= SrcSize.y)
        return;

    float y = NvPlaneY.Load(int3(p, 0));
    float2 uv = (float2(p) + 0.5) / float2(SrcSize);
    float2 c = NvPlaneUV.SampleLevel(LinearClamp, uv, 0);

    Output[p] = float4(YuvToRgb(y, c.x, c.y), 1.0);
}
