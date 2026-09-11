// The Direct3D 11 renderer behind rbgc_videofx.
//
// One class, created per video surface, destroyed with it. Everything it owns
// is a COM object and everything is released in the destructor; there is no
// global state except the shader bytecode, which is compiled in.
//
// THE DEVICE IS SOMETIMES NOT OURS
// --------------------------------
// With hardware decoding on, the frames arrive as ID3D11Texture2D owned by
// FFmpeg's own device. Rather than creating a second device and copying
// between them through a shared handle, this renderer ADOPTS FFmpeg's device,
// which it gets by asking the first texture it is handed.
//
// That removes interop entirely, and it buys something less obvious for free:
// with one device, D3D11's own per-resource dependency tracking stops FFmpeg
// recycling a decoder array slice while the GPU is still reading it. Across
// two devices that would need an explicit fence, and getting it wrong produces
// a picture that tears occasionally under load -- the worst kind of bug to go
// looking for.
//
// So the device is created lazily: on the first submit, from the frame, once
// it is known whether there is a texture to adopt one from.

#pragma once

#include <d3d11_4.h>
#include <dxgi1_6.h>
#include <wrl/client.h>

#include <cstdint>
#include <string>

#include "videofx.h"

using Microsoft::WRL::ComPtr;

// NVIDIA's video-processing extension interface. This -- not an SDK, not
// NVAPI, not Maxine -- is how an application asks for RTX Video Super
// Resolution. It is the same path VLC, mpv and Chromium use, which is why
// nothing NVIDIA has to be redistributed with this client.
//
// {D43CE1B3-1F4B-48AC-BAEE-C3C2532E5E06}
extern const GUID kNvidiaPPEInterfaceGUID;

struct NvidiaStreamExtension
{
    uint32_t version;   // kNvidiaPPEInterfaceVersion == 1
    uint32_t method;    // 1 = super resolution, 2 = true HDR
    uint32_t enable;
};

constexpr uint32_t kNvidiaPPEVersion = 0x1;
constexpr uint32_t kNvidiaPPESuperResolution = 1;

// How the current frame is being fed in.
enum class SourceKind
{
    None,
    Software,   // three yuv420p planes, uploaded
    Hardware,   // an ID3D11Texture2D from the decoder
};

class D3D11Renderer
{
public:
    D3D11Renderer() = default;
    ~D3D11Renderer();

    D3D11Renderer(const D3D11Renderer&) = delete;
    D3D11Renderer& operator=(const D3D11Renderer&) = delete;

    rbgc_status Create(HWND window, int32_t mode);
    rbgc_status SetMode(int32_t mode);
    rbgc_status SetSharpness(float attenuation);
    rbgc_status SetBackdrop(uint32_t rgb);
    rbgc_status Submit(const rbgc_frame* frame, rbgc_result* result);
    rbgc_status Repaint();

    // Tests only, and off by default: FLIP_DISCARD means the back buffer is
    // gone after Present, so without this a readback sees black whether the
    // renderer worked or not.
    rbgc_status DebugCapture(bool enable);

    // Tests only. Maps a staging copy for reading, i.e. it waits for the GPU.
    rbgc_status DebugReadback(void* pixels, uint32_t capacity,
                              int32_t* width, int32_t* height);

    const char* LastError() const { return last_error_.c_str(); }

    // Fills `caps` by creating a throwaway device and asking it questions.
    static rbgc_status Probe(rbgc_caps* caps);

private:
    rbgc_status EnsureDevice(const rbgc_frame* frame);
    rbgc_status EnsureSwapChain();
    rbgc_status EnsureShaders();
    rbgc_status EnsureSourceTextures(int32_t width, int32_t height, bool need_nv12);
    rbgc_status EnsureScratch(int32_t width, int32_t height);
    rbgc_status EnsureVideoProcessor(int32_t in_w, int32_t in_h,
                                     int32_t out_w, int32_t out_h);

    rbgc_status UploadSoftware(const rbgc_frame* frame);
    rbgc_status ConvertToRgba(const rbgc_frame* frame);
    rbgc_status BltHardware(const rbgc_frame* frame,
                            ID3D11Texture2D* destination,
                            const RECT& src_rect, const RECT& dst_rect,
                            bool want_super_resolution);

    void WriteConstants(const rbgc_frame* frame, const rbgc_blit& blit,
                        int32_t off_x, int32_t off_y, bool easu_pass);
    // RCAS reads EASU's output, not the source picture, so its source
    // rectangle is the scratch texture's origin rather than the blit's.
    void WriteRcasConstants(const rbgc_frame* frame, const rbgc_blit& blit,
                            int32_t off_x, int32_t off_y);
    void DrawBlitLanczos(const rbgc_frame* frame, const rbgc_blit& blit,
                         int32_t off_x, int32_t off_y);
    void DrawBlitFsr(const rbgc_frame* frame, const rbgc_blit& blit,
                     int32_t off_x, int32_t off_y);
    rbgc_status DrawBlitVsr(const rbgc_frame* frame, const rbgc_blit& blit,
                            int32_t off_x, int32_t off_y);

    rbgc_status UploadOverlay(const rbgc_frame* frame);
    void DrawOverlay(const rbgc_frame* frame);

    void BeginTiming();
    void EndTiming(rbgc_result* result);

    void Fail(rbgc_status status, const char* what, HRESULT hr = S_OK);
    void Teardown();

    // -- state ---------------------------------------------------------------

    HWND window_ = nullptr;
    int32_t mode_ = RBGC_MODE_LANCZOS;
    float rcas_attenuation_ = 0.5f;
    uint32_t backdrop_ = 0x000000;
    std::string last_error_;

    ComPtr<ID3D11Device> device_;
    ComPtr<ID3D11DeviceContext> context_;
    // True when the device came from a decoder texture rather than from us.
    bool adopted_device_ = false;

    ComPtr<IDXGISwapChain1> swap_chain_;
    ComPtr<ID3D11Texture2D> back_buffer_;
    ComPtr<ID3D11RenderTargetView> back_rtv_;
    ComPtr<ID3D11UnorderedAccessView> back_uav_;
    int32_t back_width_ = 0;
    int32_t back_height_ = 0;
    bool allow_tearing_ = false;

    // Shaders, compiled in. See shaders_generated.h.
    ComPtr<ID3D11ComputeShader> cs_convert_yuv_;
    ComPtr<ID3D11ComputeShader> cs_convert_nv12_;
    ComPtr<ID3D11ComputeShader> cs_easu_;
    ComPtr<ID3D11ComputeShader> cs_rcas_;
    ComPtr<ID3D11ComputeShader> cs_lanczos_;
    ComPtr<ID3D11VertexShader> vs_overlay_;
    ComPtr<ID3D11PixelShader> ps_overlay_;
    ComPtr<ID3D11Buffer> constants_;
    ComPtr<ID3D11Buffer> overlay_constants_;
    ComPtr<ID3D11SamplerState> sampler_linear_;
    ComPtr<ID3D11SamplerState> sampler_point_;
    ComPtr<ID3D11BlendState> blend_premultiplied_;

    // Software upload: one dynamic texture per plane.
    ComPtr<ID3D11Texture2D> plane_tex_[3];
    ComPtr<ID3D11ShaderResourceView> plane_srv_[3];
    // NV12, for the video processor when the decode was in software.
    //
    // Two textures rather than one, and the reason is a D3D11 rule that shows
    // up only as E_INVALIDARG: a DYNAMIC texture cannot back a video processor
    // input view. So the upload goes to a mappable one and is copied to a
    // DEFAULT one the processor will accept. The copy is GPU-to-GPU and only
    // happens on the software-decode + RTX VSR combination, which is the one
    // nobody should be using anyway -- if the hardware can do VSR it can
    // almost certainly decode too, and then neither texture exists.
    ComPtr<ID3D11Texture2D> nv12_tex_;
    ComPtr<ID3D11Texture2D> nv12_vp_tex_;
    int32_t source_width_ = 0;
    int32_t source_height_ = 0;
    bool source_is_nv12_ = false;

    // The converted picture every scaler samples from.
    ComPtr<ID3D11Texture2D> rgba_tex_;
    ComPtr<ID3D11ShaderResourceView> rgba_srv_;
    ComPtr<ID3D11UnorderedAccessView> rgba_uav_;

    // EASU's output, which RCAS then sharpens.
    ComPtr<ID3D11Texture2D> scratch_tex_;
    ComPtr<ID3D11ShaderResourceView> scratch_srv_;
    ComPtr<ID3D11UnorderedAccessView> scratch_uav_;
    int32_t scratch_width_ = 0;
    int32_t scratch_height_ = 0;

    ComPtr<ID3D11Texture2D> overlay_tex_;
    ComPtr<ID3D11ShaderResourceView> overlay_srv_;
    int32_t overlay_width_ = 0;
    int32_t overlay_height_ = 0;
    uint32_t overlay_version_ = 0;
    bool overlay_valid_ = false;

    // Video processor, for hardware frames and for RTX VSR.
    ComPtr<ID3D11VideoDevice> video_device_;
    ComPtr<ID3D11VideoContext> video_context_;
    ComPtr<ID3D11VideoProcessor> video_processor_;
    ComPtr<ID3D11VideoProcessorEnumerator> video_enumerator_;
    int32_t vp_in_width_ = 0;
    int32_t vp_in_height_ = 0;
    int32_t vp_out_width_ = 0;
    int32_t vp_out_height_ = 0;
    bool vsr_requested_ = false;
    bool vsr_accepted_ = false;

    // Timestamps, read back a frame late and never waited on. See EndTiming.
    static constexpr int kTimingSlots = 3;
    ComPtr<ID3D11Query> q_disjoint_[kTimingSlots];
    ComPtr<ID3D11Query> q_begin_[kTimingSlots];
    ComPtr<ID3D11Query> q_end_[kTimingSlots];
    int timing_slot_ = 0;
    bool timing_pending_[kTimingSlots] = {false, false, false};
    float last_gpu_ms_ = -1.0f;

    // The last frame's geometry, so a repaint with no new picture works.
    bool have_last_frame_ = false;

    // Tests only; see DebugCapture.
    bool debug_capture_ = false;
    ComPtr<ID3D11Texture2D> capture_tex_;
};
