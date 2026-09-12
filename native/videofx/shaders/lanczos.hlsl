// The control: a plain, high-quality resample with no enhancement at all.
//
// This mode exists to be measured against. Without it, "is FSR 1 better than
// nothing?" and "what does RTX VSR actually cost?" are both unanswerable,
// because Off differs from the enhanced modes in how it *presents* as well as
// in what it does to the pixels -- Off blits through QPainter on the GUI
// thread, and everything here presents through a swapchain. Comparing FSR to
// Off would measure both changes at once and attribute the result to the
// wrong one.
//
// Lanczos with a = 2, in one pass, 16 taps.
//
// a = 3 is the more familiar choice and is genuinely a little sharper, but it
// is 36 taps in a single pass or two passes plus an intermediate, and this is
// the mode whose whole purpose is to be a cheap, honest baseline. a = 2 keeps
// it to one dispatch and one texture, which is also what makes its cost
// straightforward to attribute.
//
// It handles downscaling too, and that is deliberate: a piece of a split
// screen can be minified while another is magnified, and the caller routes
// both here rather than running super-resolution on an image it is shrinking.
// The window widens with the scale factor so minification pre-filters instead
// of aliasing.

#include "common.hlsli"

Texture2D<float4> Source : register(t0);
RWTexture2D<float4> Output : register(u0);

static const float kPi = 3.14159265358979323846;
static const float kA = 2.0;

float Sinc(float x)
{
    if (abs(x) < 1e-5)
        return 1.0;
    float px = kPi * x;
    return sin(px) / px;
}

float Lanczos(float x)
{
    x = abs(x);
    if (x >= kA)
        return 0.0;
    return Sinc(x) * Sinc(x / kA);
}

[numthreads(8, 8, 1)]
void main(uint3 tid : SV_DispatchThreadID)
{
    int2 out_px = int2(tid.xy);
    if (out_px.x >= DstSize.x || out_px.y >= DstSize.y)
        return;

    float2 scale = float2(SrcSize) / float2(DstSize);

    // Centre of this output pixel, in the source piece's own texel space.
    float2 centre = (float2(out_px) + 0.5) * scale - 0.5;

    // When minifying, the filter must widen or it samples a fraction of the
    // texels it is meant to be averaging and aliases. When magnifying it must
    // not, or the picture goes soft for no reason.
    float2 support = max(scale, float2(1.0, 1.0));
    float2 inv_support = 1.0 / support;

    float3 acc = float3(0.0, 0.0, 0.0);
    float weight_sum = 0.0;

    int2 first = int2(floor(centre - kA * support));
    int2 last = int2(ceil(centre + kA * support));

    // Bounded so a pathological scale cannot turn one output pixel into an
    // unbounded loop. At magnification this is 4x4; heavy minification is
    // clamped and softens slightly rather than stalling the GPU.
    first = max(first, centre - 8.0);
    last = min(last, centre + 8.0);

    for (int y = first.y; y <= last.y; ++y)
    {
        float wy = Lanczos((float(y) - centre.y) * inv_support.y);
        if (wy == 0.0)
            continue;

        for (int x = first.x; x <= last.x; ++x)
        {
            float wx = Lanczos((float(x) - centre.x) * inv_support.x);
            if (wx == 0.0)
                continue;

            // Clamped to this piece's own rectangle, not to the texture's.
            // Two pieces of a split screen are adjacent in the source, so
            // clamping to the texture would sample the neighbouring player's
            // picture at the seam. Same reasoning as easu.hlsl.
            int2 sp = clamp(int2(x, y) + SrcOffset,
                            SrcOffset,
                            SrcOffset + SrcSize - 1);
            float w = wx * wy;
            acc += Source.Load(int3(sp, 0)).rgb * w;
            weight_sum += w;
        }
    }

    // Lanczos has negative lobes, so the sum can undershoot; saturate rather
    // than letting a ringing overshoot wrap round to black.
    float3 colour = weight_sum > 0.0 ? acc / weight_sum : float3(0.0, 0.0, 0.0);
    Output[out_px + DstOffset] = float4(saturate(colour), 1.0);
}
