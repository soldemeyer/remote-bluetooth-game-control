/*
 * rbgc_videofx -- optional GPU video enhancement for the RBGC client.
 *
 * A flat C ABI, loaded with ctypes. Deliberately not a CPython extension:
 * one binary then works across every Python this project supports (3.11-3.14)
 * and needs no PyInstaller or Nuitka hook beyond being copied next to the exe.
 *
 * THE RULES THIS LIBRARY OBEYS, and none of them are optional:
 *
 *  1. It never calls into Python. No callbacks, no PyGILState_Ensure. Every
 *     entry point is pull-style and returns a status code. The caller loads
 *     this with ctypes.WinDLL/CDLL -- which release the GIL around the call --
 *     and *never* PyDLL, which does not. Swapping that one word would invert
 *     the whole feature into a GIL disaster whose only symptom is the client's
 *     500 Hz input loop growing a tail.
 *
 *  2. rbgc_submit is SYNCHRONOUS and copies. When it returns, this library
 *     holds no pointer into caller memory. There is no render thread, no
 *     queue, no refcount and no handshake, so a use-after-free is not
 *     constructible rather than merely absent. The caller's frame comes
 *     straight out of FFmpeg's buffer pool and may still be a reference frame;
 *     reading it inside the call is fine, holding it would pin the pool.
 *
 *  3. Nothing here ever fails fatally. Every failure is a status code, and the
 *     caller's answer to all of them is the same: fall back to its existing
 *     software path and keep the stream running. A frozen picture with healthy
 *     counters is a worse outcome than no enhancement.
 *
 *  4. No frame is ever queued. The caller presents the newest state it has and
 *     drops anything older, which is the same discipline its UDP datapath uses.
 *
 * Structs carry `struct_size` so a newer caller and an older library can agree
 * on what is present. Every field is fixed-width and the layout is what a C
 * compiler produces with default packing on x86-64 and aarch64.
 */

#ifndef RBGC_VIDEOFX_H
#define RBGC_VIDEOFX_H

#include <stdint.h>

#if defined(_WIN32)
#  define RBGC_API __declspec(dllexport)
#  define RBGC_CALL __cdecl
#else
#  define RBGC_API __attribute__((visibility("default")))
#  define RBGC_CALL
#endif

#ifdef __cplusplus
extern "C" {
#endif

/* ------------------------------------------------------------------ status */

typedef enum rbgc_status {
    RBGC_OK = 0,
    RBGC_ERR_ARG = 1,          /* a caller mistake; the caller has a bug     */
    RBGC_ERR_UNSUPPORTED = 2,  /* this machine cannot do it; not a fault     */
    RBGC_ERR_DEVICE = 3,       /* device or swapchain creation failed        */
    RBGC_ERR_DEVICE_LOST = 4,  /* TDR, driver update, GPU reset -- recreate  */
    RBGC_ERR_SHADER = 5,
    RBGC_ERR_RESOURCE = 6,
    RBGC_ERR_WINDOW = 7,       /* the window handle died underneath us       */
    RBGC_ERR_INTERNAL = 8
} rbgc_status;

/* -------------------------------------------------------------- what to do */

typedef enum rbgc_mode {
    /* Present through the GPU, scale with a plain high-quality filter, add
     * nothing. The control the other two are measured against, and on its own
     * it already removes the caller's GIL-held blit. */
    RBGC_MODE_LANCZOS = 0,
    RBGC_MODE_FSR1 = 1,        /* FidelityFX Super Resolution 1: EASU + RCAS */
    RBGC_MODE_RTX_VSR = 2      /* NVIDIA RTX Video Super Resolution          */
} rbgc_mode;

/* What actually happened to the last frame.
 *
 * Reported per frame because the decision is per piece and per resize: one
 * cell of a four-way split may be an upscale while another is a downscale.
 * An enhancement pass that silently does nothing is indistinguishable from
 * one that is working, which is the failure this whole project keeps
 * rediscovering -- so the caller displays this rather than what it asked for.
 */
typedef enum rbgc_path {
    RBGC_PATH_NONE = 0,
    RBGC_PATH_COPY = 1,        /* 1:1 -- no scale, and deliberately no RCAS  */
    RBGC_PATH_LANCZOS = 2,
    RBGC_PATH_EASU_RCAS = 3,
    RBGC_PATH_VSR = 4,         /* requested; see the note on rbgc_caps       */
    RBGC_PATH_DOWNSCALE = 5    /* output smaller than input: no SR is run    */
} rbgc_path;

/* ------------------------------------------------------------ capabilities */

#define RBGC_NAME_MAX 128
#define RBGC_SHORT_MAX 40
#define RBGC_REASON_MAX 160

typedef struct rbgc_caps {
    uint32_t struct_size;

    int32_t usable;            /* a device and swapchain can be created      */
    int32_t fsr1;              /* compute, typed UAV store, resources made   */
    int32_t rtx_vsr;           /* the driver ACCEPTED the extension          */
    int32_t hw_decode;         /* a hardware decoder is present and usable   */

    uint32_t vendor_id;        /* 0x10DE NVIDIA, 0x1002 AMD, 0x8086 Intel    */
    uint32_t device_id;
    uint64_t driver_version;   /* encoded; render with rbgc_driver_string    */

    char gpu_name[RBGC_NAME_MAX];
    char backend[RBGC_SHORT_MAX];   /* "Direct3D 11", "Vulkan"               */
    char driver[RBGC_SHORT_MAX];    /* human readable, e.g. "581.42"         */

    /* Empty when the corresponding flag is set. Shown verbatim in the GUI
     * next to the disabled control, so it is written for a player, not for a
     * developer: "Requires a supported NVIDIA RTX GPU", never an HRESULT. */
    char reason_usable[RBGC_REASON_MAX];
    char reason_fsr1[RBGC_REASON_MAX];
    char reason_rtx_vsr[RBGC_REASON_MAX];
    char reason_hw_decode[RBGC_REASON_MAX];
} rbgc_caps;

/* ------------------------------------------------------------------ frames */

/* One piece of the uploaded picture, and where it lands.
 *
 * `src` is normalised inside the UPLOADED RECTANGLE, not inside the decoded
 * frame: this library is handed only the pixels that are actually shown, which
 * is what makes "crop before you upscale" structural. `dst` is in pixels
 * inside the composed picture, which the library then centres in the window.
 */
typedef struct rbgc_blit {
    float src[4];              /* x, y, w, h -- 0..1                         */
    int32_t dst[4];            /* x, y, w, h -- composed pixels              */
} rbgc_blit;

typedef struct rbgc_frame {
    uint32_t struct_size;

    /* SOFTWARE DECODE: three yuv420p planes, already offset to the uploaded
     * rectangle's origin. NULL plane[0] means the hardware path below. */
    const void* plane[3];
    int32_t stride[3];

    /* HARDWARE DECODE: an ID3D11Texture2D* (D3D11) or VkImage (Vulkan) the
     * decoder owns, plus the array slice within it. The texture is NV12 and
     * is BIND_DECODER only, so it is read through the video processor rather
     * than a shader resource view -- see the backend.
     *
     * Its height is macroblock-aligned and therefore LARGER than the frame:
     * 1088 for 1080. Every rectangle is computed against src_height, never
     * against the texture, or the bottom eight rows are somebody else's
     * memory. */
    void* hw_texture;
    uint32_t hw_slice;

    /* The uploaded rectangle's own size, in luma pixels. */
    int32_t src_width;
    int32_t src_height;

    /* AVColorSpace / AVColorRange, straight off the decoded frame. Passed
     * rather than assumed: a hardcoded BT.709 makes the picture shift colour
     * when the mode changes, which gets reported as "FSR looks washed out". */
    int32_t colorspace;
    int32_t color_range;

    int32_t composed_width;
    int32_t composed_height;

    const rbgc_blit* blits;
    int32_t blit_count;

    /* Premultiplied RGBA8, or NULL. The caller's overlay -- its latency
     * overlay and its floating control bar, which a native child window would
     * otherwise draw over. Re-uploaded only when `overlay_version` changes. */
    const void* overlay;
    int32_t overlay_stride;
    int32_t overlay_x;
    int32_t overlay_y;
    int32_t overlay_width;
    int32_t overlay_height;
    uint32_t overlay_version;
} rbgc_frame;

typedef struct rbgc_result {
    uint32_t struct_size;
    int32_t path;              /* rbgc_path                                  */
    int32_t present_skipped;   /* window occluded: drawn nothing, not an error */
    float gpu_ms;              /* enhancement only; < 0 when not yet readable */
    int32_t output_width;      /* the backbuffer we actually presented into  */
    int32_t output_height;
} rbgc_result;

/* --------------------------------------------------------------------- api */

typedef struct rbgc_renderer rbgc_renderer;

/* Version of this library, as "major.minor.patch". Never NULL. */
RBGC_API const char* RBGC_CALL rbgc_version(void);

/* What this machine can do. Creates a device, probes, and tears it down, so
 * it costs 50-150 ms and belongs on a worker thread, not a UI thread.
 * Returns RBGC_OK even when nothing is supported -- the answer is in `out`. */
RBGC_API rbgc_status RBGC_CALL rbgc_probe(rbgc_caps* out);

/* Create a renderer presenting into `native_window`: an HWND on Windows, or a
 * wl_surface*/xcb_window_t on Linux (see rbgc_create_linux). */
RBGC_API rbgc_status RBGC_CALL rbgc_create(
    void* native_window, int32_t mode, rbgc_renderer** out);

/* Change mode without recreating the device or disturbing the stream. Called
 * between submits, on the caller's decode thread. */
RBGC_API rbgc_status RBGC_CALL rbgc_set_mode(rbgc_renderer* r, int32_t mode);

/* RCAS attenuation for FSR 1, in FidelityFX's own units: 0 is sharpest and 2
 * is softest. The caller maps a 0-100 slider onto it and never shows this
 * number to anyone. */
RBGC_API rbgc_status RBGC_CALL rbgc_set_sharpness(rbgc_renderer* r, float attenuation);

/* The colour the letterbox is cleared to, as 0xRRGGBB. The caller's theme is
 * switchable at runtime, so this is pushed rather than baked in. */
RBGC_API rbgc_status RBGC_CALL rbgc_set_backdrop(rbgc_renderer* r, uint32_t rgb);

/* Upload, enhance, present. Synchronous; see rule 2 at the top of this file.
 * `result` may be NULL. */
RBGC_API rbgc_status RBGC_CALL rbgc_submit(
    rbgc_renderer* r, const rbgc_frame* frame, rbgc_result* result);

/* Re-present the last frame -- for when only the overlay changed, or the
 * window was resized while the stream was idle. */
RBGC_API rbgc_status RBGC_CALL rbgc_repaint(rbgc_renderer* r);

/* The last failure, as English. Never NULL; empty when there has been none. */
RBGC_API const char* RBGC_CALL rbgc_last_error(rbgc_renderer* r);

RBGC_API void RBGC_CALL rbgc_destroy(rbgc_renderer* r);

#ifdef __cplusplus
}  /* extern "C" */
#endif

#endif /* RBGC_VIDEOFX_H */
