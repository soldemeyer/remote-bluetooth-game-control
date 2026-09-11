// The client's own overlay, composited into the presented frame.
//
// A native child window draws above every Qt sibling, so the client's latency
// readout and its floating control bar would be hidden the moment the GPU path
// turns on. Hiding the latency overlay in exactly the mode where somebody is
// trying to prove presentation got faster is self-defeating, so it is drawn
// here instead.
//
// The client renders both into one RGBA image on its GUI thread -- only when
// the text or the bar actually changes, which is at most ten times a second --
// and hands it over with a version number. Nothing is re-uploaded while it is
// unchanged.
//
// PREMULTIPLIED alpha. Qt's Format_RGBA8888_Premultiplied maps to
// DXGI_FORMAT_R8G8B8A8_UNORM with no channel swizzle and no conversion, and
// premultiplied is what a straight `src + dst*(1-a)` blend wants. Taking
// Format_ARGB32 instead would cost a per-pixel swizzle on the CPU and give a
// dark fringe everywhere the overlay is translucent -- which, for a design
// whose panels are deliberately glassy, is everywhere.

struct VSOut
{
    float4 pos : SV_POSITION;
    float2 uv  : TEXCOORD0;
};

cbuffer OverlayConstants : register(b0)
{
    // The overlay's rectangle in normalised device coordinates, as
    // (x0, y0, x1, y1). Computed on the CPU so this shader has no idea how
    // big the back buffer is.
    float4 Rect;
};

// A full-screen triangle's worth of vertices, indexed rather than fetched:
// three vertices, no vertex buffer, no input layout to create or bind.
VSOut vs_main(uint id : SV_VertexID)
{
    VSOut o;
    float2 corner = float2((id << 1) & 2, id & 2);   // (0,0) (2,0) (0,2)
    o.uv = corner;
    o.pos = float4(
        lerp(Rect.x, Rect.z, corner.x),
        lerp(Rect.y, Rect.w, corner.y),
        0.0,
        1.0);
    return o;
}

Texture2D<float4> Overlay : register(t0);
SamplerState PointClamp : register(s0);

float4 ps_main(VSOut i) : SV_TARGET
{
    // Point sampling, not linear. The overlay is authored at exactly the back
    // buffer's pixel density by the client, which already knows the device
    // pixel ratio; filtering it would only blur text that is pixel-aligned.
    return Overlay.SampleLevel(PointClamp, i.uv, 0);
}
