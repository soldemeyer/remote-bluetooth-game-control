// The exported C API. Thin on purpose: every entry point validates its
// arguments, forwards to the backend, and turns anything unexpected into a
// status code.
//
// NOTHING HERE MAY THROW ACROSS THE BOUNDARY. The caller is CPython via
// ctypes, which has no idea what a C++ exception is; one escaping would
// terminate the process, and it would do so on the client's decode thread with
// no traceback and nothing in any log. So every entry point is wrapped, and
// the catch-all returns RBGC_ERR_INTERNAL rather than letting anything out.
//
// The version string is the ABI contract. client/media/videofx.py refuses a
// library whose major version is not the one it was built against, because a
// struct-layout mismatch is not a crash anybody can debug from the traceback.

#include "videofx.h"
#include "d3d11_backend.h"

#include <new>

namespace {

// Bumped when anything in videofx.h changes incompatibly. Must match
// ABI_MAJOR in client/media/videofx.py.
const char kVersion[] = "1.0.0";

D3D11Renderer* AsRenderer(rbgc_renderer* handle)
{
    return reinterpret_cast<D3D11Renderer*>(handle);
}

// Big enough that a caller built against an older header, which stops short of
// a field added since, is still recognisable. Smaller than that and we would
// be reading fields it never wrote.
bool FrameLooksSane(const rbgc_frame* frame)
{
    if (!frame) return false;
    if (frame->struct_size < sizeof(rbgc_frame)) return false;
    if (frame->src_width <= 0 || frame->src_height <= 0) return false;
    if (frame->blit_count <= 0 || frame->blit_count > 64) return false;
    if (!frame->blits) return false;
    if (frame->composed_width <= 0 || frame->composed_height <= 0) return false;
    // Either three planes or a texture, never neither.
    if (!frame->hw_texture && !frame->plane[0]) return false;
    return true;
}

}  // namespace


extern "C" {

RBGC_API const char* RBGC_CALL rbgc_version(void)
{
    return kVersion;
}

RBGC_API rbgc_status RBGC_CALL rbgc_probe(rbgc_caps* out)
{
    if (!out) return RBGC_ERR_ARG;
    if (out->struct_size < sizeof(rbgc_caps)) return RBGC_ERR_ARG;

    // Zeroed here rather than trusted from the caller, so an unsupported
    // feature reports false even if the probe bails out early.
    const uint32_t size = out->struct_size;
    memset(out, 0, sizeof(rbgc_caps));
    out->struct_size = size;

    try
    {
        return D3D11Renderer::Probe(out);
    }
    catch (...)
    {
        return RBGC_ERR_INTERNAL;
    }
}

RBGC_API rbgc_status RBGC_CALL rbgc_create(
    void* native_window, int32_t mode, rbgc_renderer** out)
{
    if (!out) return RBGC_ERR_ARG;
    *out = nullptr;
    if (!native_window) return RBGC_ERR_ARG;
    if (mode < RBGC_MODE_LANCZOS || mode > RBGC_MODE_RTX_VSR) return RBGC_ERR_ARG;

    try
    {
        auto* renderer = new (std::nothrow) D3D11Renderer();
        if (!renderer) return RBGC_ERR_RESOURCE;

        rbgc_status status = renderer->Create(
            static_cast<HWND>(native_window), mode);
        if (status != RBGC_OK)
        {
            delete renderer;
            return status;
        }
        *out = reinterpret_cast<rbgc_renderer*>(renderer);
        return RBGC_OK;
    }
    catch (...)
    {
        return RBGC_ERR_INTERNAL;
    }
}

RBGC_API rbgc_status RBGC_CALL rbgc_set_mode(rbgc_renderer* r, int32_t mode)
{
    if (!r) return RBGC_ERR_ARG;
    try
    {
        return AsRenderer(r)->SetMode(mode);
    }
    catch (...)
    {
        return RBGC_ERR_INTERNAL;
    }
}

RBGC_API rbgc_status RBGC_CALL rbgc_set_sharpness(rbgc_renderer* r, float attenuation)
{
    if (!r) return RBGC_ERR_ARG;
    try
    {
        return AsRenderer(r)->SetSharpness(attenuation);
    }
    catch (...)
    {
        return RBGC_ERR_INTERNAL;
    }
}

RBGC_API rbgc_status RBGC_CALL rbgc_set_backdrop(rbgc_renderer* r, uint32_t rgb)
{
    if (!r) return RBGC_ERR_ARG;
    try
    {
        return AsRenderer(r)->SetBackdrop(rgb);
    }
    catch (...)
    {
        return RBGC_ERR_INTERNAL;
    }
}

RBGC_API rbgc_status RBGC_CALL rbgc_submit(
    rbgc_renderer* r, const rbgc_frame* frame, rbgc_result* result)
{
    if (!r) return RBGC_ERR_ARG;
    if (!FrameLooksSane(frame)) return RBGC_ERR_ARG;
    if (result && result->struct_size < sizeof(rbgc_result)) return RBGC_ERR_ARG;

    try
    {
        return AsRenderer(r)->Submit(frame, result);
    }
    catch (...)
    {
        return RBGC_ERR_INTERNAL;
    }
}

RBGC_API rbgc_status RBGC_CALL rbgc_repaint(rbgc_renderer* r)
{
    if (!r) return RBGC_ERR_ARG;
    try
    {
        return AsRenderer(r)->Repaint();
    }
    catch (...)
    {
        return RBGC_ERR_INTERNAL;
    }
}

RBGC_API const char* RBGC_CALL rbgc_last_error(rbgc_renderer* r)
{
    if (!r) return "";
    try
    {
        return AsRenderer(r)->LastError();
    }
    catch (...)
    {
        return "";
    }
}

RBGC_API rbgc_status RBGC_CALL rbgc_debug_capture(rbgc_renderer* r, int32_t enable)
{
    if (!r) return RBGC_ERR_ARG;
    try
    {
        return AsRenderer(r)->DebugCapture(enable != 0);
    }
    catch (...)
    {
        return RBGC_ERR_INTERNAL;
    }
}

RBGC_API rbgc_status RBGC_CALL rbgc_debug_readback(
    rbgc_renderer* r, void* pixels, uint32_t capacity,
    int32_t* width, int32_t* height)
{
    if (!r) return RBGC_ERR_ARG;
    try
    {
        return AsRenderer(r)->DebugReadback(pixels, capacity, width, height);
    }
    catch (...)
    {
        return RBGC_ERR_INTERNAL;
    }
}

RBGC_API void RBGC_CALL rbgc_destroy(rbgc_renderer* r)
{
    if (!r) return;
    try
    {
        delete AsRenderer(r);
    }
    catch (...)
    {
        // Nothing useful to do, and nowhere to report it. Swallowed rather
        // than allowed out: this is called from Python during teardown, often
        // while the interpreter is already shutting down.
    }
}

}  // extern "C"
