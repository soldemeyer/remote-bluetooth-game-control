// The per-frame half of the Direct3D 11 renderer: upload, enhance, present.
//
// Split from d3d11_backend.cpp, which owns the device and the resources.
// Everything here runs once per decoded frame on the caller's decode thread,
// so it is the code that has to stay cheap.
//
// THREE PATHS THROUGH THIS FILE, and they are chosen per piece, per frame:
//
//   Lanczos      convert -> one compute dispatch -> the back buffer
//   FSR 1        convert -> EASU -> RCAS -> the back buffer
//   RTX VSR      the video processor, straight to the back buffer
//
// and a fourth that is not a path at all: when a piece would be drawn no
// larger than it arrived, no super-resolution runs. That decision is here
// rather than in the caller because it is per piece -- one cell of a four-way
// split can be a magnification while another is a reduction -- and because it
// changes on every window resize.

#include "d3d11_backend.h"
#include "d3d11_shared.h"

#define A_CPU 1
#include "third_party/ffx_a.h"
#include "third_party/ffx_fsr1.h"

#include <algorithm>
#include <cstring>

namespace {

// FSR 1's reference dispatch covers a 16x16 tile per thread group.
uint32_t FsrGroups(int32_t extent)
{
    return DivRoundUp(static_cast<uint32_t>(std::max(extent, 1)), 16u);
}

}  // namespace


// ------------------------------------------------------------ the pieces

void D3D11Renderer::DrawBlitLanczos(const rbgc_frame* frame, const rbgc_blit& blit,
                                    int32_t off_x, int32_t off_y)
{
    WriteConstants(frame, blit, off_x, off_y, /*easu_pass=*/false);

    ID3D11ShaderResourceView* srv = rgba_srv_.Get();
    ID3D11UnorderedAccessView* uav = back_uav_.Get();
    ID3D11Buffer* cb = constants_.Get();

    context_->CSSetShader(cs_lanczos_.Get(), nullptr, 0);
    context_->CSSetShaderResources(0, 1, &srv);
    context_->CSSetUnorderedAccessViews(0, 1, &uav, nullptr);
    context_->CSSetConstantBuffers(0, 1, &cb);
    context_->Dispatch(DivRoundUp(blit.dst[2], 8), DivRoundUp(blit.dst[3], 8), 1);

    ID3D11ShaderResourceView* none_srv = nullptr;
    ID3D11UnorderedAccessView* none_uav = nullptr;
    context_->CSSetShaderResources(0, 1, &none_srv);
    context_->CSSetUnorderedAccessViews(0, 1, &none_uav, nullptr);
}

void D3D11Renderer::DrawBlitFsr(const rbgc_frame* frame, const rbgc_blit& blit,
                                int32_t off_x, int32_t off_y)
{
    // EASU: the piece, at its native size, into the scratch texture at the
    // destination size.
    WriteConstants(frame, blit, off_x, off_y, /*easu_pass=*/true);

    ID3D11ShaderResourceView* srv = rgba_srv_.Get();
    ID3D11UnorderedAccessView* uav = scratch_uav_.Get();
    ID3D11Buffer* cb = constants_.Get();

    context_->CSSetShader(cs_easu_.Get(), nullptr, 0);
    context_->CSSetShaderResources(0, 1, &srv);
    context_->CSSetUnorderedAccessViews(0, 1, &uav, nullptr);
    context_->CSSetConstantBuffers(0, 1, &cb);
    context_->CSSetSamplers(0, 1, sampler_linear_.GetAddressOf());
    context_->Dispatch(FsrGroups(blit.dst[2]), FsrGroups(blit.dst[3]), 1);

    ID3D11ShaderResourceView* none_srv = nullptr;
    ID3D11UnorderedAccessView* none_uav = nullptr;
    context_->CSSetShaderResources(0, 1, &none_srv);
    context_->CSSetUnorderedAccessViews(0, 1, &none_uav, nullptr);

    // RCAS: the scratch texture, sharpened, straight into the back buffer at
    // the piece's offset. No intermediate and no copy -- one fewer
    // full-resolution round trip through memory per piece per frame.
    //
    // Its source is the EASU output, which starts at (0,0) and is exactly the
    // destination size, so the constants are rewritten with that rectangle
    // rather than the blit's.
    WriteRcasConstants(frame, blit, off_x, off_y);

    srv = scratch_srv_.Get();
    uav = back_uav_.Get();
    context_->CSSetShader(cs_rcas_.Get(), nullptr, 0);
    context_->CSSetShaderResources(0, 1, &srv);
    context_->CSSetUnorderedAccessViews(0, 1, &uav, nullptr);
    context_->CSSetConstantBuffers(0, 1, &cb);
    context_->Dispatch(FsrGroups(blit.dst[2]), FsrGroups(blit.dst[3]), 1);

    context_->CSSetShaderResources(0, 1, &none_srv);
    context_->CSSetUnorderedAccessViews(0, 1, &none_uav, nullptr);
}

rbgc_status D3D11Renderer::DrawBlitVsr(const rbgc_frame* frame, const rbgc_blit& blit,
                                       int32_t off_x, int32_t off_y)
{
    const int32_t tex_w = std::max(source_width_, 1);
    const int32_t tex_h = std::max(source_height_, 1);

    RECT src{};
    src.left = static_cast<LONG>(blit.src[0] * tex_w + 0.5f);
    src.top = static_cast<LONG>(blit.src[1] * tex_h + 0.5f);
    src.right = std::min<LONG>(tex_w, src.left + static_cast<LONG>(blit.src[2] * tex_w + 0.5f));
    src.bottom = std::min<LONG>(tex_h, src.top + static_cast<LONG>(blit.src[3] * tex_h + 0.5f));

    RECT dst{};
    dst.left = blit.dst[0] + off_x;
    dst.top = blit.dst[1] + off_y;
    dst.right = dst.left + blit.dst[2];
    dst.bottom = dst.top + blit.dst[3];

    return BltHardware(frame, back_buffer_.Get(), src, dst, /*want_super_resolution=*/true);
}


// ---------------------------------------------------------------- overlay

rbgc_status D3D11Renderer::UploadOverlay(const rbgc_frame* frame)
{
    if (!frame->overlay || frame->overlay_width <= 0 || frame->overlay_height <= 0)
    {
        overlay_valid_ = false;
        return RBGC_OK;
    }

    // Re-uploaded only when the caller says it changed. The client renders it
    // at most ten times a second and usually far less, so this is nearly
    // always a no-op -- which is the point of carrying a version at all.
    if (overlay_valid_ && frame->overlay_version == overlay_version_
        && frame->overlay_width == overlay_width_
        && frame->overlay_height == overlay_height_)
        return RBGC_OK;

    if (!overlay_tex_ || frame->overlay_width != overlay_width_
        || frame->overlay_height != overlay_height_)
    {
        overlay_srv_.Reset();
        overlay_tex_.Reset();

        D3D11_TEXTURE2D_DESC desc{};
        desc.Width = frame->overlay_width;
        desc.Height = frame->overlay_height;
        desc.MipLevels = 1;
        desc.ArraySize = 1;
        desc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
        desc.SampleDesc.Count = 1;
        desc.Usage = D3D11_USAGE_DYNAMIC;
        desc.BindFlags = D3D11_BIND_SHADER_RESOURCE;
        desc.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
        if (FAILED(device_->CreateTexture2D(&desc, nullptr, &overlay_tex_))
            || FAILED(device_->CreateShaderResourceView(overlay_tex_.Get(), nullptr,
                                                        &overlay_srv_)))
        {
            overlay_valid_ = false;
            return RBGC_OK;  // an overlay is a nicety; never fail the frame
        }
        overlay_width_ = frame->overlay_width;
        overlay_height_ = frame->overlay_height;
    }

    D3D11_MAPPED_SUBRESOURCE mapped{};
    if (FAILED(context_->Map(overlay_tex_.Get(), 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped)))
    {
        overlay_valid_ = false;
        return RBGC_OK;
    }
    auto* dst = static_cast<uint8_t*>(mapped.pData);
    const auto* src = static_cast<const uint8_t*>(frame->overlay);
    const size_t row_bytes = static_cast<size_t>(frame->overlay_width) * 4;
    for (int32_t row = 0; row < frame->overlay_height; ++row)
        memcpy(dst + row * mapped.RowPitch, src + row * frame->overlay_stride, row_bytes);
    context_->Unmap(overlay_tex_.Get(), 0);

    overlay_version_ = frame->overlay_version;
    overlay_valid_ = true;
    return RBGC_OK;
}

void D3D11Renderer::DrawOverlay(const rbgc_frame* frame)
{
    if (!overlay_valid_ || !overlay_srv_ || back_width_ <= 0 || back_height_ <= 0)
        return;

    // The overlay's rectangle, in normalised device coordinates. Y is flipped
    // because NDC counts up from the bottom and the caller thinks in pixels
    // from the top, like every other rectangle in this feature.
    const float x0 = (2.0f * frame->overlay_x / back_width_) - 1.0f;
    const float x1 = (2.0f * (frame->overlay_x + frame->overlay_width) / back_width_) - 1.0f;
    const float y0 = 1.0f - (2.0f * frame->overlay_y / back_height_);
    const float y1 = 1.0f - (2.0f * (frame->overlay_y + frame->overlay_height) / back_height_);

    struct { float rect[4]; } cb{{x0, y0, x1, y1}};
    D3D11_MAPPED_SUBRESOURCE mapped{};
    if (SUCCEEDED(context_->Map(overlay_constants_.Get(), 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped)))
    {
        memcpy(mapped.pData, &cb, sizeof(cb));
        context_->Unmap(overlay_constants_.Get(), 0);
    }

    D3D11_VIEWPORT viewport{};
    viewport.Width = static_cast<float>(back_width_);
    viewport.Height = static_cast<float>(back_height_);
    viewport.MaxDepth = 1.0f;

    ID3D11RenderTargetView* rtv = back_rtv_.Get();
    ID3D11ShaderResourceView* srv = overlay_srv_.Get();
    ID3D11Buffer* constants = overlay_constants_.Get();
    ID3D11SamplerState* sampler = sampler_point_.Get();
    const float blend_factor[4] = {0, 0, 0, 0};

    context_->OMSetRenderTargets(1, &rtv, nullptr);
    context_->RSSetViewports(1, &viewport);
    context_->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    context_->IASetInputLayout(nullptr);
    context_->VSSetShader(vs_overlay_.Get(), nullptr, 0);
    context_->VSSetConstantBuffers(0, 1, &constants);
    context_->PSSetShader(ps_overlay_.Get(), nullptr, 0);
    context_->PSSetShaderResources(0, 1, &srv);
    context_->PSSetSamplers(0, 1, &sampler);
    context_->OMSetBlendState(blend_premultiplied_.Get(), blend_factor, 0xFFFFFFFF);
    context_->Draw(3, 0);

    ID3D11ShaderResourceView* none = nullptr;
    ID3D11RenderTargetView* no_rtv = nullptr;
    context_->PSSetShaderResources(0, 1, &none);
    context_->OMSetRenderTargets(1, &no_rtv, nullptr);
    context_->OMSetBlendState(nullptr, blend_factor, 0xFFFFFFFF);
}


// ----------------------------------------------------------------- timing

void D3D11Renderer::BeginTiming()
{
    if (!q_disjoint_[timing_slot_])
        return;
    context_->Begin(q_disjoint_[timing_slot_].Get());
    context_->End(q_begin_[timing_slot_].Get());
}

void D3D11Renderer::EndTiming(rbgc_result* result)
{
    if (q_disjoint_[timing_slot_])
    {
        context_->End(q_end_[timing_slot_].Get());
        context_->End(q_disjoint_[timing_slot_].Get());
        timing_pending_[timing_slot_] = true;
    }

    // Read back the OLDEST slot, never this frame's, and never block. A
    // blocking GetData here would stall the decode thread on the GPU every
    // frame -- which would make the measurement itself the largest thing being
    // measured, and add exactly the latency this feature exists to remove.
    const int oldest = (timing_slot_ + 1) % kTimingSlots;
    if (timing_pending_[oldest])
    {
        D3D11_QUERY_DATA_TIMESTAMP_DISJOINT disjoint{};
        UINT64 begin = 0;
        UINT64 end = 0;
        const UINT flags = D3D11_ASYNC_GETDATA_DONOTFLUSH;
        if (context_->GetData(q_disjoint_[oldest].Get(), &disjoint, sizeof(disjoint), flags) == S_OK
            && context_->GetData(q_begin_[oldest].Get(), &begin, sizeof(begin), flags) == S_OK
            && context_->GetData(q_end_[oldest].Get(), &end, sizeof(end), flags) == S_OK)
        {
            timing_pending_[oldest] = false;
            if (!disjoint.Disjoint && disjoint.Frequency != 0 && end > begin)
                last_gpu_ms_ = static_cast<float>(
                    (end - begin) * 1000.0 / static_cast<double>(disjoint.Frequency));
        }
        // Not ready is the ordinary case for a frame or two. Left pending.
    }

    timing_slot_ = (timing_slot_ + 1) % kTimingSlots;
    if (result)
        result->gpu_ms = last_gpu_ms_;
}


// ----------------------------------------------------------------- submit

rbgc_status D3D11Renderer::Submit(const rbgc_frame* frame, rbgc_result* result)
{
    if (!frame || frame->blit_count <= 0 || !frame->blits)
        return RBGC_ERR_ARG;

    if (result)
    {
        result->path = RBGC_PATH_NONE;
        result->present_skipped = 0;
        result->gpu_ms = -1.0f;
        result->output_width = 0;
        result->output_height = 0;
    }

    rbgc_status status = EnsureDevice(frame);
    if (status != RBGC_OK) return status;
    status = EnsureSwapChain();
    if (status != RBGC_OK) return status;

    const bool hardware = frame->hw_texture != nullptr;
    const bool vsr = mode_ == RBGC_MODE_RTX_VSR;

    // The source. RTX VSR reads NV12 through the video processor, so a
    // software frame has to be interleaved into one; every other path wants
    // the RGBA the shaders sample.
    const bool need_nv12 = vsr && !hardware;
    status = EnsureSourceTextures(frame->src_width, frame->src_height, need_nv12);
    if (status != RBGC_OK) return status;

    if (!hardware)
    {
        status = UploadSoftware(frame);
        if (status != RBGC_OK) return status;
        if (!need_nv12)
        {
            status = ConvertToRgba(frame);
            if (status != RBGC_OK) return status;
        }
    }
    else if (!vsr)
    {
        // A decoder texture is BIND_DECODER only, so no shader can sample it.
        // The video processor can, and the same call converts NV12 to RGB and
        // applies the crop -- so this replaces the conversion pass rather than
        // adding one.
        RECT src{0, 0, frame->src_width, frame->src_height};
        RECT dst{0, 0, frame->src_width, frame->src_height};
        status = BltHardware(frame, rgba_tex_.Get(), src, dst, /*want_super_resolution=*/false);
        if (status != RBGC_OK) return status;
    }

    // Scratch, big enough for the largest piece.
    int32_t widest = 0;
    int32_t tallest = 0;
    for (int32_t i = 0; i < frame->blit_count; ++i)
    {
        widest = std::max(widest, frame->blits[i].dst[2]);
        tallest = std::max(tallest, frame->blits[i].dst[3]);
    }
    if (mode_ == RBGC_MODE_FSR1)
    {
        status = EnsureScratch(widest, tallest);
        if (status != RBGC_OK) return status;
    }

    // The composed picture is centred in the window, and whatever is left over
    // is the letterbox.
    const int32_t off_x = (back_width_ - frame->composed_width) / 2;
    const int32_t off_y = (back_height_ - frame->composed_height) / 2;

    const float clear[4] = {
        ((backdrop_ >> 16) & 0xFF) / 255.0f,
        ((backdrop_ >> 8) & 0xFF) / 255.0f,
        (backdrop_ & 0xFF) / 255.0f,
        1.0f,
    };
    context_->ClearRenderTargetView(back_rtv_.Get(), clear);

    BeginTiming();

    int32_t chosen = RBGC_PATH_NONE;
    for (int32_t i = 0; i < frame->blit_count; ++i)
    {
        const rbgc_blit& blit = frame->blits[i];
        const int32_t src_w = std::max(1, static_cast<int32_t>(blit.src[2] * source_width_ + 0.5f));
        const int32_t src_h = std::max(1, static_cast<int32_t>(blit.src[3] * source_height_ + 0.5f));

        // "If the output is no bigger than the input, do not run
        // super-resolution." Decided here, per piece and per frame: one cell
        // of a four-way split can be a magnification while another is a
        // reduction, and the answer changes on every window resize.
        //
        // At 1:1 that means no RCAS either. Sharpening a picture nobody asked
        // to have scaled is a different feature, and turning it on quietly
        // would change what every player sees.
        const bool magnifying = blit.dst[2] > src_w + 1 || blit.dst[3] > src_h + 1;

        if (vsr && magnifying)
        {
            status = DrawBlitVsr(frame, blit, off_x, off_y);
            if (status != RBGC_OK) return status;
            chosen = std::max(chosen, static_cast<int32_t>(RBGC_PATH_VSR));
        }
        else if (mode_ == RBGC_MODE_FSR1 && magnifying)
        {
            DrawBlitFsr(frame, blit, off_x, off_y);
            chosen = std::max(chosen, static_cast<int32_t>(RBGC_PATH_EASU_RCAS));
        }
        else if (vsr)
        {
            // VSR mode, but this piece is not being enlarged. The video
            // processor still has to do the conversion, just without asking
            // for super-resolution.
            const int32_t tex_w = std::max(source_width_, 1);
            const int32_t tex_h = std::max(source_height_, 1);
            RECT src{
                static_cast<LONG>(blit.src[0] * tex_w + 0.5f),
                static_cast<LONG>(blit.src[1] * tex_h + 0.5f),
                0, 0};
            src.right = std::min<LONG>(tex_w, src.left + src_w);
            src.bottom = std::min<LONG>(tex_h, src.top + src_h);
            RECT dst{blit.dst[0] + off_x, blit.dst[1] + off_y, 0, 0};
            dst.right = dst.left + blit.dst[2];
            dst.bottom = dst.top + blit.dst[3];
            status = BltHardware(frame, back_buffer_.Get(), src, dst, false);
            if (status != RBGC_OK) return status;
            chosen = std::max(chosen,
                              static_cast<int32_t>(magnifying ? RBGC_PATH_COPY
                                                              : RBGC_PATH_DOWNSCALE));
        }
        else
        {
            DrawBlitLanczos(frame, blit, off_x, off_y);
            const int32_t path = magnifying
                                     ? RBGC_PATH_LANCZOS
                                     : (blit.dst[2] == src_w && blit.dst[3] == src_h
                                            ? RBGC_PATH_COPY
                                            : RBGC_PATH_DOWNSCALE);
            chosen = std::max(chosen, path);
        }
    }

    EndTiming(result);

    UploadOverlay(frame);
    DrawOverlay(frame);

    have_last_frame_ = true;

    // Tests only, and normally skipped entirely. FLIP_DISCARD throws the back
    // buffer away at Present, so a readback afterwards can only ever see
    // black -- which is indistinguishable from a renderer that drew nothing.
    if (debug_capture_)
    {
        D3D11_TEXTURE2D_DESC desc{};
        back_buffer_->GetDesc(&desc);
        desc.BindFlags = D3D11_BIND_SHADER_RESOURCE;
        desc.MiscFlags = 0;
        D3D11_TEXTURE2D_DESC have{};
        if (capture_tex_) capture_tex_->GetDesc(&have);
        if (!capture_tex_ || have.Width != desc.Width || have.Height != desc.Height)
        {
            capture_tex_.Reset();
            device_->CreateTexture2D(&desc, nullptr, &capture_tex_);
        }
        if (capture_tex_)
            context_->CopyResource(capture_tex_.Get(), back_buffer_.Get());
    }

    // SyncInterval 0: never wait for vblank, never queue. DXGI's own frame
    // latency is already pinned to 1 on the device.
    const UINT flags = allow_tearing_ ? DXGI_PRESENT_ALLOW_TEARING : 0;
    HRESULT hr = swap_chain_->Present(0, flags);

    if (hr == DXGI_STATUS_OCCLUDED)
    {
        // The window is completely hidden. Presenting again immediately would
        // spin a core at SyncInterval 0, so this frame is simply not shown --
        // and decoding carries on, because the stream must not stall behind a
        // minimised window.
        if (result) result->present_skipped = 1;
    }
    else if (hr == DXGI_ERROR_DEVICE_REMOVED || hr == DXGI_ERROR_DEVICE_RESET)
    {
        HRESULT reason = device_ ? device_->GetDeviceRemovedReason() : hr;
        Fail(RBGC_ERR_DEVICE_LOST, "the graphics device was lost", reason);
        return RBGC_ERR_DEVICE_LOST;
    }
    else if (FAILED(hr))
    {
        Fail(RBGC_ERR_DEVICE, "presenting failed", hr);
        return RBGC_ERR_DEVICE;
    }

    if (result)
    {
        result->path = chosen;
        result->output_width = back_width_;
        result->output_height = back_height_;
    }
    return RBGC_OK;
}

rbgc_status D3D11Renderer::Repaint()
{
    // Only the overlay changed, or the window was resized while the stream was
    // idle. There is no new picture to build, so this presents what is already
    // in the back buffer rather than re-running the whole chain.
    if (!swap_chain_ || !have_last_frame_)
        return RBGC_OK;

    rbgc_status status = EnsureSwapChain();
    if (status != RBGC_OK)
        return status;

    const UINT flags = allow_tearing_ ? DXGI_PRESENT_ALLOW_TEARING : 0;
    HRESULT hr = swap_chain_->Present(0, flags);
    if (hr == DXGI_ERROR_DEVICE_REMOVED || hr == DXGI_ERROR_DEVICE_RESET)
    {
        Fail(RBGC_ERR_DEVICE_LOST, "the graphics device was lost", hr);
        return RBGC_ERR_DEVICE_LOST;
    }
    return RBGC_OK;
}


// ------------------------------------------------------------- the knobs

rbgc_status D3D11Renderer::SetMode(int32_t mode)
{
    if (mode < RBGC_MODE_LANCZOS || mode > RBGC_MODE_RTX_VSR)
        return RBGC_ERR_ARG;
    if (mode == mode_)
        return RBGC_OK;

    mode_ = mode;
    // The video processor is configured for one kind of work; asking it for a
    // different one without rebuilding leaves the extension state stale. It is
    // rebuilt on the next frame, which is a clean boundary -- between submits,
    // with nothing in flight.
    video_processor_.Reset();
    video_enumerator_.Reset();
    vp_in_width_ = vp_in_height_ = vp_out_width_ = vp_out_height_ = 0;
    vsr_accepted_ = false;
    vsr_requested_ = false;
    // The source textures may need to change shape too (NV12 for VSR, three
    // planes otherwise), which EnsureSourceTextures works out from the mode.
    source_width_ = source_height_ = 0;
    return RBGC_OK;
}

rbgc_status D3D11Renderer::SetSharpness(float attenuation)
{
    // FidelityFX's units: 0 is maximum sharpness and larger is softer, in
    // stops of halving. The client maps a slider onto this and never shows
    // the number to anyone.
    if (!(attenuation >= 0.0f) || attenuation > 4.0f)
        return RBGC_ERR_ARG;
    rcas_attenuation_ = attenuation;
    return RBGC_OK;
}

rbgc_status D3D11Renderer::SetBackdrop(uint32_t rgb)
{
    backdrop_ = rgb & 0x00FFFFFFu;
    return RBGC_OK;
}


// ------------------------------------------------------------- for tests

rbgc_status D3D11Renderer::DebugReadback(void* pixels, uint32_t capacity,
                                         int32_t* width, int32_t* height)
{
    // Deliberately the only place in this library that reads GPU memory back.
    // It copies the back buffer to a staging texture and maps it, which waits
    // for everything queued ahead of it -- fine for a test, ruinous per frame.
    if (width) *width = back_width_;
    if (height) *height = back_height_;
    ID3D11Texture2D* source = capture_tex_ ? capture_tex_.Get() : back_buffer_.Get();
    if (!source || back_width_ <= 0 || back_height_ <= 0)
        return RBGC_ERR_RESOURCE;

    const uint32_t needed = static_cast<uint32_t>(back_width_) *
                            static_cast<uint32_t>(back_height_) * 4u;
    if (!pixels || capacity < needed)
        return RBGC_ERR_ARG;

    D3D11_TEXTURE2D_DESC desc{};
    source->GetDesc(&desc);
    desc.Usage = D3D11_USAGE_STAGING;
    desc.BindFlags = 0;
    desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    desc.MiscFlags = 0;

    ComPtr<ID3D11Texture2D> staging;
    if (FAILED(device_->CreateTexture2D(&desc, nullptr, &staging)))
    {
        Fail(RBGC_ERR_RESOURCE, "could not create a staging texture");
        return RBGC_ERR_RESOURCE;
    }
    context_->CopyResource(staging.Get(), source);

    D3D11_MAPPED_SUBRESOURCE mapped{};
    if (FAILED(context_->Map(staging.Get(), 0, D3D11_MAP_READ, 0, &mapped)))
    {
        Fail(RBGC_ERR_RESOURCE, "could not map the staging texture");
        return RBGC_ERR_RESOURCE;
    }

    auto* out = static_cast<uint8_t*>(pixels);
    const auto* src = static_cast<const uint8_t*>(mapped.pData);
    const size_t row_bytes = static_cast<size_t>(back_width_) * 4;
    for (int32_t row = 0; row < back_height_; ++row)
    {
        memcpy(out + static_cast<size_t>(row) * row_bytes,
               src + static_cast<size_t>(row) * mapped.RowPitch,
               row_bytes);
    }
    context_->Unmap(staging.Get(), 0);
    return RBGC_OK;
}


rbgc_status D3D11Renderer::DebugCapture(bool enable)
{
    debug_capture_ = enable;
    if (!enable)
        capture_tex_.Reset();
    return RBGC_OK;
}
