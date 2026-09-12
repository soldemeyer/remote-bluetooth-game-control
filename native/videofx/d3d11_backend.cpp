#include "d3d11_backend.h"

// FidelityFX's CPU half: FsrEasuCon and FsrRcasCon, which pack the constants
// the shaders unpack. Same headers the shaders include, so the two halves
// cannot drift.
#define A_CPU 1
#include "third_party/ffx_a.h"
#include "third_party/ffx_fsr1.h"

#include "d3d11_shared.h"
#include "shaders_generated.h"

#include <algorithm>
#include <cstring>

// {D43CE1B3-1F4B-48AC-BAEE-C3C2532E5E06}
const GUID kNvidiaPPEInterfaceGUID = {
    0xD43CE1B3, 0x1F4B, 0x48AC,
    {0xBA, 0xEE, 0xC3, 0xC2, 0x53, 0x2E, 0x5E, 0x06}};

namespace {

void CopyString(char* dst, size_t cap, const char* src)
{
    if (!dst || cap == 0) return;
    if (!src) { dst[0] = '\0'; return; }
    strncpy_s(dst, cap, src, _TRUNCATE);
}

void CopyWide(char* dst, size_t cap, const wchar_t* src)
{
    if (!dst || cap == 0) return;
    dst[0] = '\0';
    if (!src) return;
    size_t written = 0;
    wcstombs_s(&written, dst, cap, src, cap - 1);
}

}  // namespace


D3D11Renderer::~D3D11Renderer()
{
    Teardown();
}

void D3D11Renderer::Fail(rbgc_status status, const char* what, HRESULT hr)
{
    char buffer[RBGC_REASON_MAX];
    if (hr != S_OK)
        snprintf(buffer, sizeof(buffer), "%s (0x%08lX)", what, static_cast<unsigned long>(hr));
    else
        snprintf(buffer, sizeof(buffer), "%s", what);
    last_error_ = buffer;
    (void)status;
}

void D3D11Renderer::Teardown()
{
    // Order matters only in that the context must not be mid-command when the
    // swap chain goes. Flush rather than wait: a wait here would block the
    // caller's decode thread on the GPU for no benefit, and everything below
    // is reference counted anyway.
    if (context_)
    {
        context_->ClearState();
        context_->Flush();
    }

    back_rtv_.Reset();
    back_uav_.Reset();
    back_buffer_.Reset();
    swap_chain_.Reset();

    for (int i = 0; i < 3; ++i)
    {
        plane_srv_[i].Reset();
        plane_tex_[i].Reset();
    }
    nv12_tex_.Reset();
    nv12_vp_tex_.Reset();
    rgba_srv_.Reset();
    rgba_uav_.Reset();
    rgba_tex_.Reset();
    scratch_srv_.Reset();
    scratch_uav_.Reset();
    scratch_tex_.Reset();
    overlay_srv_.Reset();
    overlay_tex_.Reset();

    vp_output_view_.Reset();
    vp_output_for_ = nullptr;
    video_processor_.Reset();
    video_enumerator_.Reset();
    video_context_.Reset();
    video_device_.Reset();

    for (int i = 0; i < kTimingSlots; ++i)
    {
        q_disjoint_[i].Reset();
        q_begin_[i].Reset();
        q_end_[i].Reset();
        timing_pending_[i] = false;
    }

    cs_convert_yuv_.Reset();
    cs_convert_nv12_.Reset();
    cs_easu_.Reset();
    cs_rcas_.Reset();
    cs_lanczos_.Reset();
    vs_overlay_.Reset();
    ps_overlay_.Reset();
    constants_.Reset();
    overlay_constants_.Reset();
    sampler_linear_.Reset();
    sampler_point_.Reset();
    blend_premultiplied_.Reset();

    context_.Reset();
    device_.Reset();
    adopted_device_ = false;
    back_width_ = back_height_ = 0;
    source_width_ = source_height_ = 0;
    scratch_width_ = scratch_height_ = 0;
    overlay_width_ = overlay_height_ = 0;
    overlay_valid_ = false;
    have_last_frame_ = false;
}


// ---------------------------------------------------------------- probing

rbgc_status D3D11Renderer::Probe(rbgc_caps* caps)
{
    if (!caps) return RBGC_ERR_ARG;

    CopyString(caps->backend, RBGC_SHORT_MAX, "Direct3D 11");

    ComPtr<ID3D11Device> device;
    ComPtr<ID3D11DeviceContext> context;
    D3D_FEATURE_LEVEL level = D3D_FEATURE_LEVEL_11_0;
    const D3D_FEATURE_LEVEL wanted[] = {D3D_FEATURE_LEVEL_11_1, D3D_FEATURE_LEVEL_11_0};

    HRESULT hr = D3D11CreateDevice(
        nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr,
        D3D11_CREATE_DEVICE_BGRA_SUPPORT | D3D11_CREATE_DEVICE_VIDEO_SUPPORT,
        wanted, ARRAYSIZE(wanted), D3D11_SDK_VERSION,
        &device, &level, &context);

    if (FAILED(hr))
    {
        CopyString(caps->reason_usable, RBGC_REASON_MAX,
                   "No Direct3D 11 graphics device is available");
        CopyString(caps->reason_fsr1, RBGC_REASON_MAX,
                   "No Direct3D 11 graphics device is available");
        CopyString(caps->reason_rtx_vsr, RBGC_REASON_MAX,
                   "No Direct3D 11 graphics device is available");
        return RBGC_OK;
    }

    // Which adapter this actually landed on, and what its driver is.
    ComPtr<IDXGIDevice> dxgi_device;
    ComPtr<IDXGIAdapter> adapter;
    if (SUCCEEDED(device.As(&dxgi_device)) && SUCCEEDED(dxgi_device->GetAdapter(&adapter)))
    {
        DXGI_ADAPTER_DESC desc{};
        if (SUCCEEDED(adapter->GetDesc(&desc)))
        {
            CopyWide(caps->gpu_name, RBGC_NAME_MAX, desc.Description);
            caps->vendor_id = desc.VendorId;
            caps->device_id = desc.DeviceId;
        }

        // The user-mode driver version, without NVAPI or any vendor SDK.
        LARGE_INTEGER umd{};
        if (SUCCEEDED(adapter->CheckInterfaceSupport(__uuidof(IDXGIDevice), &umd)))
        {
            caps->driver_version = static_cast<uint64_t>(umd.QuadPart);
            const uint64_t v = static_cast<uint64_t>(umd.QuadPart);
            char text[RBGC_SHORT_MAX];
            snprintf(text, sizeof(text), "%u.%u.%u.%u",
                     static_cast<unsigned>((v >> 48) & 0xFFFF),
                     static_cast<unsigned>((v >> 32) & 0xFFFF),
                     static_cast<unsigned>((v >> 16) & 0xFFFF),
                     static_cast<unsigned>(v & 0xFFFF));
            CopyString(caps->driver, RBGC_SHORT_MAX, text);
        }
    }

    caps->usable = 1;

    // -- FSR 1 -------------------------------------------------------------
    //
    // Asked of the device rather than inferred from the vendor. FSR 1 is
    // vendor-agnostic by design and should light up on NVIDIA, AMD and Intel
    // alike; what it actually needs is compute and a typed UAV store, and
    // both are questions D3D11 answers directly.
    if (level < D3D_FEATURE_LEVEL_11_0)
    {
        CopyString(caps->reason_fsr1, RBGC_REASON_MAX,
                   "Requires a Direct3D 11 feature level 11_0 graphics card");
    }
    else
    {
        D3D11_FEATURE_DATA_FORMAT_SUPPORT2 uav{};
        uav.InFormat = DXGI_FORMAT_R8G8B8A8_UNORM;
        const bool typed_store =
            SUCCEEDED(device->CheckFeatureSupport(D3D11_FEATURE_FORMAT_SUPPORT2,
                                                  &uav, sizeof(uav)))
            && (uav.OutFormatSupport2 & D3D11_FORMAT_SUPPORT2_UAV_TYPED_STORE);

        // Not merely "is it allowed" -- build the real thing. A capability bit
        // that is set while creation fails is exactly how an option ends up in
        // a GUI that cannot run it.
        ComPtr<ID3D11ComputeShader> probe_shader;
        const bool shader_ok = SUCCEEDED(
            device->CreateComputeShader(kEasu, kEasuSize, nullptr, &probe_shader));

        if (typed_store && shader_ok)
            caps->fsr1 = 1;
        else if (!typed_store)
            CopyString(caps->reason_fsr1, RBGC_REASON_MAX,
                       "This graphics card cannot write the required texture format");
        else
            CopyString(caps->reason_fsr1, RBGC_REASON_MAX,
                       "The FSR 1 shaders could not be loaded on this graphics card");
    }

    // -- RTX Video Super Resolution ---------------------------------------
    //
    // The probe is the real API call. Vendor is checked first only to avoid a
    // false positive from another vendor's video-processing extension that
    // happens to accept the same GUID.
    if (caps->vendor_id != 0x10DE)
    {
        CopyString(caps->reason_rtx_vsr, RBGC_REASON_MAX,
                   "Requires a supported NVIDIA RTX graphics card");
    }
    else
    {
        ComPtr<ID3D11VideoDevice> video_device;
        ComPtr<ID3D11VideoContext> video_context;
        ComPtr<ID3D11VideoProcessorEnumerator> enumerator;
        ComPtr<ID3D11VideoProcessor> processor;

        D3D11_VIDEO_PROCESSOR_CONTENT_DESC desc{};
        desc.InputFrameFormat = D3D11_VIDEO_FRAME_FORMAT_PROGRESSIVE;
        desc.InputWidth = 1920;
        desc.InputHeight = 1080;
        desc.OutputWidth = 3840;
        desc.OutputHeight = 2160;
        desc.Usage = D3D11_VIDEO_USAGE_PLAYBACK_NORMAL;

        bool accepted = false;
        if (SUCCEEDED(device.As(&video_device))
            && SUCCEEDED(context.As(&video_context))
            && SUCCEEDED(video_device->CreateVideoProcessorEnumerator(&desc, &enumerator))
            && SUCCEEDED(video_device->CreateVideoProcessor(enumerator.Get(), 0, &processor)))
        {
            NvidiaStreamExtension ext{kNvidiaPPEVersion, kNvidiaPPESuperResolution, 1};
            accepted = SUCCEEDED(video_context->VideoProcessorSetStreamExtension(
                processor.Get(), 0, &kNvidiaPPEInterfaceGUID, sizeof(ext), &ext));
        }

        if (accepted)
            caps->rtx_vsr = 1;
        else
            CopyString(caps->reason_rtx_vsr, RBGC_REASON_MAX,
                       "This NVIDIA driver did not accept RTX Video Super "
                       "Resolution; a newer driver may be required");
    }

    return RBGC_OK;
}


// ---------------------------------------------------------------- creation

rbgc_status D3D11Renderer::Create(HWND window, int32_t mode)
{
    if (!window || !IsWindow(window))
    {
        Fail(RBGC_ERR_WINDOW, "the window handle is not valid");
        return RBGC_ERR_WINDOW;
    }
    window_ = window;
    mode_ = mode;
    // The device is created on the first submit, not here: with hardware
    // decoding on it is adopted from the decoder's texture, and until a frame
    // arrives there is nothing to adopt. See the header.
    return RBGC_OK;
}

rbgc_status D3D11Renderer::EnsureDevice(const rbgc_frame* frame)
{
    if (device_)
        return RBGC_OK;

    if (frame && frame->hw_texture)
    {
        // Adopt the decoder's device. This is what makes hardware decode
        // zero-copy: its textures are already on this device, so there is
        // nothing to share and nothing to synchronise.
        auto* texture = static_cast<ID3D11Texture2D*>(frame->hw_texture);
        ComPtr<ID3D11Device> owner;
        texture->GetDevice(&owner);
        if (!owner)
        {
            Fail(RBGC_ERR_DEVICE, "the decoder's texture has no device");
            return RBGC_ERR_DEVICE;
        }
        device_ = owner;
        device_->GetImmediateContext(&context_);
        adopted_device_ = true;

        // FFmpeg creates its device with multithread protection, because it
        // decodes on its own threads. Assert it rather than assume: without
        // it, our submits and its decodes race on one context and the
        // corruption is intermittent and impossible to attribute.
        ComPtr<ID3D10Multithread> mt;
        if (SUCCEEDED(context_.As(&mt)))
            mt->SetMultithreadProtected(TRUE);
    }
    else
    {
        D3D_FEATURE_LEVEL level = D3D_FEATURE_LEVEL_11_0;
        const D3D_FEATURE_LEVEL wanted[] = {D3D_FEATURE_LEVEL_11_1, D3D_FEATURE_LEVEL_11_0};
        HRESULT hr = D3D11CreateDevice(
            nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr,
            D3D11_CREATE_DEVICE_BGRA_SUPPORT | D3D11_CREATE_DEVICE_VIDEO_SUPPORT,
            wanted, ARRAYSIZE(wanted), D3D11_SDK_VERSION,
            &device_, &level, &context_);
        if (FAILED(hr))
        {
            Fail(RBGC_ERR_DEVICE, "could not create a Direct3D 11 device", hr);
            return RBGC_ERR_DEVICE;
        }
        adopted_device_ = false;
    }

    // One frame in flight, never three. DXGI queues by default, and a queue
    // here is exactly the added latency this whole client is built to avoid.
    ComPtr<IDXGIDevice1> dxgi_device;
    if (SUCCEEDED(device_.As(&dxgi_device)))
        dxgi_device->SetMaximumFrameLatency(1);

    return EnsureShaders();
}

rbgc_status D3D11Renderer::EnsureShaders()
{
    if (cs_easu_)
        return RBGC_OK;

    struct { const uint8_t* blob; size_t size; ID3D11ComputeShader** out; const char* name; }
    compute[] = {
        {kConvertYuv420, kConvertYuv420Size, cs_convert_yuv_.GetAddressOf(), "convert"},
        {kConvertNv12, kConvertNv12Size, cs_convert_nv12_.GetAddressOf(), "convert-nv12"},
        {kEasu, kEasuSize, cs_easu_.GetAddressOf(), "EASU"},
        {kRcas, kRcasSize, cs_rcas_.GetAddressOf(), "RCAS"},
        {kLanczos, kLanczosSize, cs_lanczos_.GetAddressOf(), "Lanczos"},
    };
    for (const auto& item : compute)
    {
        HRESULT hr = device_->CreateComputeShader(item.blob, item.size, nullptr, item.out);
        if (FAILED(hr))
        {
            Fail(RBGC_ERR_SHADER, item.name, hr);
            return RBGC_ERR_SHADER;
        }
    }

    HRESULT hr = device_->CreateVertexShader(kOverlayVS, kOverlayVSSize, nullptr, &vs_overlay_);
    if (SUCCEEDED(hr))
        hr = device_->CreatePixelShader(kOverlayPS, kOverlayPSSize, nullptr, &ps_overlay_);
    if (FAILED(hr))
    {
        Fail(RBGC_ERR_SHADER, "overlay shaders", hr);
        return RBGC_ERR_SHADER;
    }

    D3D11_BUFFER_DESC cb{};
    cb.ByteWidth = sizeof(Constants);
    cb.Usage = D3D11_USAGE_DYNAMIC;
    cb.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    cb.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    if (FAILED(device_->CreateBuffer(&cb, nullptr, &constants_)))
    {
        Fail(RBGC_ERR_RESOURCE, "constant buffer");
        return RBGC_ERR_RESOURCE;
    }
    cb.ByteWidth = sizeof(OverlayConstants);
    if (FAILED(device_->CreateBuffer(&cb, nullptr, &overlay_constants_)))
    {
        Fail(RBGC_ERR_RESOURCE, "overlay constant buffer");
        return RBGC_ERR_RESOURCE;
    }

    D3D11_SAMPLER_DESC sampler{};
    sampler.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR;
    sampler.AddressU = sampler.AddressV = sampler.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    sampler.MaxLOD = D3D11_FLOAT32_MAX;
    device_->CreateSamplerState(&sampler, &sampler_linear_);
    sampler.Filter = D3D11_FILTER_MIN_MAG_MIP_POINT;
    device_->CreateSamplerState(&sampler, &sampler_point_);

    // Premultiplied alpha: src + dst*(1-a). The overlay is produced by Qt as
    // Format_RGBA8888_Premultiplied, so no conversion and no swizzle.
    D3D11_BLEND_DESC blend{};
    blend.RenderTarget[0].BlendEnable = TRUE;
    blend.RenderTarget[0].SrcBlend = D3D11_BLEND_ONE;
    blend.RenderTarget[0].DestBlend = D3D11_BLEND_INV_SRC_ALPHA;
    blend.RenderTarget[0].BlendOp = D3D11_BLEND_OP_ADD;
    blend.RenderTarget[0].SrcBlendAlpha = D3D11_BLEND_ONE;
    blend.RenderTarget[0].DestBlendAlpha = D3D11_BLEND_INV_SRC_ALPHA;
    blend.RenderTarget[0].BlendOpAlpha = D3D11_BLEND_OP_ADD;
    blend.RenderTarget[0].RenderTargetWriteMask = D3D11_COLOR_WRITE_ENABLE_ALL;
    device_->CreateBlendState(&blend, &blend_premultiplied_);

    D3D11_QUERY_DESC query{};
    for (int i = 0; i < kTimingSlots; ++i)
    {
        query.Query = D3D11_QUERY_TIMESTAMP_DISJOINT;
        device_->CreateQuery(&query, &q_disjoint_[i]);
        query.Query = D3D11_QUERY_TIMESTAMP;
        device_->CreateQuery(&query, &q_begin_[i]);
        device_->CreateQuery(&query, &q_end_[i]);
    }

    return RBGC_OK;
}

rbgc_status D3D11Renderer::EnsureSwapChain()
{
    RECT client{};
    if (!window_ || !IsWindow(window_) || !GetClientRect(window_, &client))
    {
        Fail(RBGC_ERR_WINDOW, "the window went away");
        return RBGC_ERR_WINDOW;
    }

    const int32_t width = std::max<LONG>(client.right - client.left, 1);
    const int32_t height = std::max<LONG>(client.bottom - client.top, 1);

    if (swap_chain_ && width == back_width_ && height == back_height_)
        return RBGC_OK;

    // The window's own client rect is the authority, not anything the caller
    // passes in. Resize, DPI change, monitor move and fullscreen then need no
    // involvement from Python at all -- which sidesteps the trap the client
    // already documents, that moving to a monitor with a different device
    // pixel ratio raises no resize event of its own.

    back_rtv_.Reset();
    back_uav_.Reset();
    back_buffer_.Reset();

    if (swap_chain_)
    {
        HRESULT hr = swap_chain_->ResizeBuffers(
            0, width, height, DXGI_FORMAT_UNKNOWN,
            allow_tearing_ ? DXGI_SWAP_CHAIN_FLAG_ALLOW_TEARING : 0);
        if (FAILED(hr))
        {
            Fail(hr == DXGI_ERROR_DEVICE_REMOVED ? RBGC_ERR_DEVICE_LOST : RBGC_ERR_DEVICE,
                 "could not resize the swap chain", hr);
            return hr == DXGI_ERROR_DEVICE_REMOVED ? RBGC_ERR_DEVICE_LOST : RBGC_ERR_DEVICE;
        }
    }
    else
    {
        ComPtr<IDXGIDevice> dxgi_device;
        ComPtr<IDXGIAdapter> adapter;
        ComPtr<IDXGIFactory2> factory;
        if (FAILED(device_.As(&dxgi_device))
            || FAILED(dxgi_device->GetAdapter(&adapter))
            || FAILED(adapter->GetParent(IID_PPV_ARGS(&factory))))
        {
            Fail(RBGC_ERR_DEVICE, "could not reach the DXGI factory");
            return RBGC_ERR_DEVICE;
        }

        // Tearing is free to ask for and correct to set, but a DWM-composited
        // child window generally will not get independent flip, so no part of
        // the latency budget depends on it.
        ComPtr<IDXGIFactory5> factory5;
        BOOL tearing = FALSE;
        if (SUCCEEDED(factory.As(&factory5)))
            factory5->CheckFeatureSupport(DXGI_FEATURE_PRESENT_ALLOW_TEARING,
                                          &tearing, sizeof(tearing));
        allow_tearing_ = tearing == TRUE;

        DXGI_SWAP_CHAIN_DESC1 desc{};
        desc.Width = width;
        desc.Height = height;
        desc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
        desc.SampleDesc.Count = 1;
        // UNORDERED_ACCESS so the last pass can write the back buffer
        // directly. Without it every piece would go through an intermediate
        // and then be copied, which is a whole extra full-resolution round
        // trip through memory per frame.
        desc.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT | DXGI_USAGE_UNORDERED_ACCESS;
        desc.BufferCount = 2;
        desc.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD;
        desc.AlphaMode = DXGI_ALPHA_MODE_IGNORE;
        // A live resize drag rubber-bands the last frame rather than flashing
        // black between ResizeBuffers calls.
        desc.Scaling = DXGI_SCALING_STRETCH;
        desc.Flags = allow_tearing_ ? DXGI_SWAP_CHAIN_FLAG_ALLOW_TEARING : 0;

        HRESULT hr = factory->CreateSwapChainForHwnd(
            device_.Get(), window_, &desc, nullptr, nullptr, &swap_chain_);
        if (FAILED(hr))
        {
            Fail(RBGC_ERR_DEVICE, "could not create a swap chain", hr);
            return RBGC_ERR_DEVICE;
        }
        // We present manually; DXGI's own Alt+Enter handling would fight the
        // client's fullscreen, which Qt owns.
        factory->MakeWindowAssociation(window_, DXGI_MWA_NO_ALT_ENTER);
    }

    HRESULT hr = swap_chain_->GetBuffer(0, IID_PPV_ARGS(&back_buffer_));
    if (SUCCEEDED(hr))
        hr = device_->CreateRenderTargetView(back_buffer_.Get(), nullptr, &back_rtv_);
    if (SUCCEEDED(hr))
        hr = device_->CreateUnorderedAccessView(back_buffer_.Get(), nullptr, &back_uav_);
    if (FAILED(hr))
    {
        Fail(RBGC_ERR_RESOURCE, "could not bind the back buffer", hr);
        return RBGC_ERR_RESOURCE;
    }

    back_width_ = width;
    back_height_ = height;
    // The cached output view names a texture that has just been replaced.
    vp_output_view_.Reset();
    vp_output_for_ = nullptr;
    return RBGC_OK;
}

rbgc_status D3D11Renderer::EnsureSourceTextures(int32_t width, int32_t height, bool need_nv12)
{
    const bool same = width == source_width_ && height == source_height_
                      && source_is_nv12_ == need_nv12;
    if (same && (plane_tex_[0] || nv12_tex_) && rgba_tex_)
        return RBGC_OK;

    for (int i = 0; i < 3; ++i)
    {
        plane_srv_[i].Reset();
        plane_tex_[i].Reset();
    }
    nv12_tex_.Reset();
    nv12_vp_tex_.Reset();
    rgba_srv_.Reset();
    rgba_uav_.Reset();
    rgba_tex_.Reset();

    D3D11_TEXTURE2D_DESC desc{};
    desc.MipLevels = 1;
    desc.ArraySize = 1;
    desc.SampleDesc.Count = 1;
    // DYNAMIC + WRITE_DISCARD rather than DEFAULT + UpdateSubresource: the
    // latter makes the driver copy into its own staging buffer first, so the
    // frame is written twice. WRITE_DISCARD hands back a fresh buffer every
    // time, which is also why no texture ring is needed here.
    desc.Usage = D3D11_USAGE_DYNAMIC;
    desc.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    desc.BindFlags = D3D11_BIND_SHADER_RESOURCE;

    if (need_nv12)
    {
        desc.Format = DXGI_FORMAT_NV12;
        desc.Width = (width + 1) & ~1;
        desc.Height = (height + 1) & ~1;
        // The mappable half stays DYNAMIC. STAGING would also be mappable,
        // but only with D3D11_MAP_WRITE, which blocks until the GPU has
        // finished with the texture -- a stall on the decode thread, every
        // frame. WRITE_DISCARD on a DYNAMIC texture hands back a fresh buffer
        // and never waits, which is the whole reason the upload path uses it.
        if (FAILED(device_->CreateTexture2D(&desc, nullptr, &nv12_tex_)))
        {
            Fail(RBGC_ERR_RESOURCE, "could not create the NV12 upload texture");
            return RBGC_ERR_RESOURCE;
        }

        // The half the video processor will accept. DEFAULT usage, no CPU
        // access: a DYNAMIC texture cannot back a video processor input view,
        // and the only symptom is E_INVALIDARG from
        // CreateVideoProcessorInputView, which says nothing about usage.
        desc.Usage = D3D11_USAGE_DEFAULT;
        desc.CPUAccessFlags = 0;
        // BIND_DECODER, which is what the hardware decoder's own textures
        // carry and what CreateVideoProcessorInputView actually wants. A
        // plain SHADER_RESOURCE texture is refused with E_INVALIDARG, which
        // says nothing about bind flags.
        desc.BindFlags = D3D11_BIND_DECODER;
        if (FAILED(device_->CreateTexture2D(&desc, nullptr, &nv12_vp_tex_)))
        {
            Fail(RBGC_ERR_RESOURCE, "could not create the NV12 source texture");
            return RBGC_ERR_RESOURCE;
        }
    }
    else
    {
        const int32_t sizes[3][2] = {
            {width, height},
            {(width + 1) / 2, (height + 1) / 2},
            {(width + 1) / 2, (height + 1) / 2},
        };
        desc.Format = DXGI_FORMAT_R8_UNORM;
        for (int i = 0; i < 3; ++i)
        {
            desc.Width = std::max(sizes[i][0], 1);
            desc.Height = std::max(sizes[i][1], 1);
            if (FAILED(device_->CreateTexture2D(&desc, nullptr, &plane_tex_[i]))
                || FAILED(device_->CreateShaderResourceView(
                       plane_tex_[i].Get(), nullptr, &plane_srv_[i])))
            {
                Fail(RBGC_ERR_RESOURCE, "could not create a plane texture");
                return RBGC_ERR_RESOURCE;
            }
        }
    }

    D3D11_TEXTURE2D_DESC rgba{};
    rgba.Width = std::max(width, 1);
    rgba.Height = std::max(height, 1);
    rgba.MipLevels = 1;
    rgba.ArraySize = 1;
    rgba.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    rgba.SampleDesc.Count = 1;
    rgba.Usage = D3D11_USAGE_DEFAULT;
    rgba.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_UNORDERED_ACCESS
                     | D3D11_BIND_RENDER_TARGET;
    if (FAILED(device_->CreateTexture2D(&rgba, nullptr, &rgba_tex_))
        || FAILED(device_->CreateShaderResourceView(rgba_tex_.Get(), nullptr, &rgba_srv_))
        || FAILED(device_->CreateUnorderedAccessView(rgba_tex_.Get(), nullptr, &rgba_uav_)))
    {
        Fail(RBGC_ERR_RESOURCE, "could not create the working picture");
        return RBGC_ERR_RESOURCE;
    }

    source_width_ = width;
    source_height_ = height;
    source_is_nv12_ = need_nv12;
    vp_output_view_.Reset();
    vp_output_for_ = nullptr;
    return RBGC_OK;
}

rbgc_status D3D11Renderer::EnsureScratch(int32_t width, int32_t height)
{
    if (scratch_tex_ && width <= scratch_width_ && height <= scratch_height_)
        return RBGC_OK;

    scratch_srv_.Reset();
    scratch_uav_.Reset();
    scratch_tex_.Reset();

    // Grown to the largest piece seen, never shrunk per frame: a split-screen
    // layout change would otherwise reallocate every texture on the frame it
    // happens, which is the one frame that can least afford it.
    const int32_t w = std::max(width, scratch_width_);
    const int32_t h = std::max(height, scratch_height_);

    D3D11_TEXTURE2D_DESC desc{};
    desc.Width = std::max(w, 1);
    desc.Height = std::max(h, 1);
    desc.MipLevels = 1;
    desc.ArraySize = 1;
    desc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    desc.SampleDesc.Count = 1;
    desc.Usage = D3D11_USAGE_DEFAULT;
    desc.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_UNORDERED_ACCESS;
    if (FAILED(device_->CreateTexture2D(&desc, nullptr, &scratch_tex_))
        || FAILED(device_->CreateShaderResourceView(scratch_tex_.Get(), nullptr, &scratch_srv_))
        || FAILED(device_->CreateUnorderedAccessView(scratch_tex_.Get(), nullptr, &scratch_uav_)))
    {
        Fail(RBGC_ERR_RESOURCE, "could not create the intermediate picture");
        return RBGC_ERR_RESOURCE;
    }
    scratch_width_ = desc.Width;
    scratch_height_ = desc.Height;
    return RBGC_OK;
}


void D3D11Renderer::WriteRcasConstants(const rbgc_frame* frame, const rbgc_blit& blit,
                                       int32_t off_x, int32_t off_y)
{
    // RCAS runs over EASU's output, which sits at the origin of the scratch
    // texture and is exactly the destination size. Its source rectangle is
    // therefore nothing to do with the blit's -- reusing the blit's would
    // sharpen a window into the wrong texture and produce a picture built
    // from the top-left corner of every piece.
    Constants c{};

    c.src_offset[0] = 0;
    c.src_offset[1] = 0;
    c.src_size[0] = std::max(1, blit.dst[2]);
    c.src_size[1] = std::max(1, blit.dst[3]);
    c.src_tex_size[0] = std::max(scratch_width_, 1);
    c.src_tex_size[1] = std::max(scratch_height_, 1);
    c.dst_offset[0] = blit.dst[0] + off_x;
    c.dst_offset[1] = blit.dst[1] + off_y;
    c.dst_size[0] = std::max(1, blit.dst[2]);
    c.dst_size[1] = std::max(1, blit.dst[3]);
    c.color_matrix = MatrixFor(frame->colorspace, frame->src_height);
    c.color_full_range = FullRangeFor(frame->color_range);
    c.backdrop = backdrop_;

    FsrRcasCon(c.rcas, rcas_attenuation_);

    D3D11_MAPPED_SUBRESOURCE mapped{};
    if (SUCCEEDED(context_->Map(constants_.Get(), 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped)))
    {
        memcpy(mapped.pData, &c, sizeof(c));
        context_->Unmap(constants_.Get(), 0);
    }
}
