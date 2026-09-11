// Getting the decoded picture onto the GPU, however it arrived.
//
// Two ways in, and the difference between them is the whole point of the
// hardware-decode option:
//
//   software   three yuv420p planes in system memory. ONE copy across the
//              bus, which is the minimum possible with a software decoder,
//              and then a compute pass converts to RGBA.
//
//   hardware   an ID3D11Texture2D the decoder already owns. Nothing is
//              copied and nothing touches system memory; the video processor
//              reads it where it lies.
//
// Everything after this file is GPU-to-GPU with no readback in either case.

#include "d3d11_backend.h"
#include "d3d11_shared.h"

// FidelityFX's CPU half: FsrEasuCon and FsrRcasCon pack the constants the
// shaders unpack. The same headers the shaders include, so the two halves
// cannot drift. Everything in them is A_STATIC, so including them in more
// than one translation unit costs nothing and defines nothing twice.
#define A_CPU 1
#include "third_party/ffx_a.h"
#include "third_party/ffx_fsr1.h"

#include <algorithm>
#include <cstring>

rbgc_status D3D11Renderer::UploadSoftware(const rbgc_frame* frame)
{
    // THE ONE COPY. A software decoder produces its picture in system memory,
    // so something has to move it across the bus.
    //
    // WRITE_DISCARD hands back a fresh buffer, which is usually
    // WRITE-COMBINED memory. Sequential forward writes to it are fast;
    // READING from it is catastrophically slow -- an overlapping memmove, a
    // read-modify-write on an unaligned tail, even a partial memset. Every
    // loop below writes forward and reads nothing, and that is a requirement
    // rather than a coincidence.
    //
    // DYNAMIC + Map beats DEFAULT + UpdateSubresource here: the latter makes
    // the driver stage the frame into its own buffer first, so it is written
    // twice. WRITE_DISCARD also means no texture ring is needed, because the
    // buffer handed back is never one the GPU is still reading.
    const int32_t width = frame->src_width;
    const int32_t height = frame->src_height;

    if (source_is_nv12_)
    {
        D3D11_MAPPED_SUBRESOURCE mapped{};
        HRESULT hr = context_->Map(nv12_tex_.Get(), 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped);
        if (FAILED(hr))
        {
            Fail(RBGC_ERR_RESOURCE, "could not map the NV12 texture", hr);
            return RBGC_ERR_RESOURCE;
        }

        auto* base = static_cast<uint8_t*>(mapped.pData);
        const auto* src_y = static_cast<const uint8_t*>(frame->plane[0]);
        for (int32_t row = 0; row < height; ++row)
            memcpy(base + row * mapped.RowPitch, src_y + row * frame->stride[0], width);

        // The chroma plane starts after RowPitch * the texture's CREATED
        // height, not the frame's. Drivers align that height, so using the
        // frame's gives correct luma and garbage colour -- a picture that
        // looks like a tracking error on an old VHS.
        D3D11_TEXTURE2D_DESC desc{};
        nv12_tex_->GetDesc(&desc);
        uint8_t* dst_uv = base + static_cast<size_t>(mapped.RowPitch) * desc.Height;

        const auto* src_u = static_cast<const uint8_t*>(frame->plane[1]);
        const auto* src_v = static_cast<const uint8_t*>(frame->plane[2]);
        const int32_t cw = (width + 1) / 2;
        const int32_t ch = (height + 1) / 2;
        for (int32_t row = 0; row < ch; ++row)
        {
            uint8_t* out = dst_uv + static_cast<size_t>(row) * mapped.RowPitch;
            const uint8_t* u = src_u + static_cast<size_t>(row) * frame->stride[1];
            const uint8_t* v = src_v + static_cast<size_t>(row) * frame->stride[2];
            for (int32_t x = 0; x < cw; ++x)
            {
                out[x * 2 + 0] = u[x];
                out[x * 2 + 1] = v[x];
            }
        }
        context_->Unmap(nv12_tex_.Get(), 0);
        // Into the texture the video processor will actually accept. See the
        // note on nv12_vp_tex_ for why there are two.
        if (nv12_vp_tex_)
            context_->CopyResource(nv12_vp_tex_.Get(), nv12_tex_.Get());
        return RBGC_OK;
    }

    const int32_t sizes[3][2] = {
        {width, height},
        {(width + 1) / 2, (height + 1) / 2},
        {(width + 1) / 2, (height + 1) / 2},
    };
    for (int i = 0; i < 3; ++i)
    {
        if (!frame->plane[i]) continue;
        D3D11_MAPPED_SUBRESOURCE mapped{};
        HRESULT hr = context_->Map(plane_tex_[i].Get(), 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped);
        if (FAILED(hr))
        {
            Fail(RBGC_ERR_RESOURCE, "could not map a plane texture", hr);
            return RBGC_ERR_RESOURCE;
        }
        auto* dst = static_cast<uint8_t*>(mapped.pData);
        const auto* src = static_cast<const uint8_t*>(frame->plane[i]);
        const int32_t rows = sizes[i][1];
        const size_t bytes = static_cast<size_t>(sizes[i][0]);
        for (int32_t row = 0; row < rows; ++row)
        {
            memcpy(dst + static_cast<size_t>(row) * mapped.RowPitch,
                   src + static_cast<size_t>(row) * frame->stride[i],
                   bytes);
        }
        context_->Unmap(plane_tex_[i].Get(), 0);
    }
    return RBGC_OK;
}

void D3D11Renderer::WriteConstants(const rbgc_frame* frame, const rbgc_blit& blit,
                                   int32_t off_x, int32_t off_y, bool easu_pass)
{
    Constants c{};

    const int32_t tex_w = std::max(source_width_, 1);
    const int32_t tex_h = std::max(source_height_, 1);

    // The blit's source rectangle, normalised inside the uploaded picture,
    // turned back into texels.
    const int32_t sx = static_cast<int32_t>(blit.src[0] * tex_w + 0.5f);
    const int32_t sy = static_cast<int32_t>(blit.src[1] * tex_h + 0.5f);
    const int32_t sw = std::max(1, static_cast<int32_t>(blit.src[2] * tex_w + 0.5f));
    const int32_t sh = std::max(1, static_cast<int32_t>(blit.src[3] * tex_h + 0.5f));

    c.src_offset[0] = sx;
    c.src_offset[1] = sy;
    c.src_size[0] = std::max(1, std::min(sw, tex_w - sx));
    c.src_size[1] = std::max(1, std::min(sh, tex_h - sy));
    c.src_tex_size[0] = tex_w;
    c.src_tex_size[1] = tex_h;
    c.dst_offset[0] = blit.dst[0] + off_x;
    c.dst_offset[1] = blit.dst[1] + off_y;
    c.dst_size[0] = std::max(1, blit.dst[2]);
    c.dst_size[1] = std::max(1, blit.dst[3]);
    c.color_matrix = MatrixFor(frame->colorspace, frame->src_height);
    c.color_full_range = FullRangeFor(frame->color_range);
    c.backdrop = backdrop_;

    if (easu_pass)
    {
        // FidelityFX's own setup, from the same header the shader includes, so
        // the two halves cannot drift.
        //
        // Viewport and texture size differ here, and FsrEasuCon takes exactly
        // that distinction: a split-screen piece is a sub-rectangle of the
        // converted picture. The origin is not one of its parameters -- the
        // reference assumes (0,0) -- so the shader adds it in the gather
        // callbacks, where it also clamps to the piece so EASU's 12 taps
        // cannot reach into the neighbouring player's half.
        FsrEasuCon(c.easu0, c.easu1, c.easu2, c.easu3,
                   static_cast<AF1>(c.src_size[0]), static_cast<AF1>(c.src_size[1]),
                   static_cast<AF1>(tex_w), static_cast<AF1>(tex_h),
                   static_cast<AF1>(c.dst_size[0]), static_cast<AF1>(c.dst_size[1]));
    }

    FsrRcasCon(c.rcas, rcas_attenuation_);

    D3D11_MAPPED_SUBRESOURCE mapped{};
    if (SUCCEEDED(context_->Map(constants_.Get(), 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped)))
    {
        memcpy(mapped.pData, &c, sizeof(c));
        context_->Unmap(constants_.Get(), 0);
    }
}

rbgc_status D3D11Renderer::ConvertToRgba(const rbgc_frame* frame)
{
    // One dispatch for the whole uploaded picture, before any piece is
    // considered. Several pieces share one source, so converting per piece
    // would convert overlapping regions repeatedly -- and for the ordinary
    // single-region case it would be identical work done under a different
    // name.
    Constants c{};
    c.src_size[0] = frame->src_width;
    c.src_size[1] = frame->src_height;
    c.src_tex_size[0] = frame->src_width;
    c.src_tex_size[1] = frame->src_height;
    c.dst_size[0] = frame->src_width;
    c.dst_size[1] = frame->src_height;
    c.color_matrix = MatrixFor(frame->colorspace, frame->src_height);
    c.color_full_range = FullRangeFor(frame->color_range);
    c.backdrop = backdrop_;

    D3D11_MAPPED_SUBRESOURCE mapped{};
    if (SUCCEEDED(context_->Map(constants_.Get(), 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped)))
    {
        memcpy(mapped.pData, &c, sizeof(c));
        context_->Unmap(constants_.Get(), 0);
    }

    ID3D11ShaderResourceView* srvs[3] = {
        plane_srv_[0].Get(), plane_srv_[1].Get(), plane_srv_[2].Get()};
    ID3D11UnorderedAccessView* uav = rgba_uav_.Get();
    ID3D11Buffer* cb = constants_.Get();
    ID3D11SamplerState* sampler = sampler_linear_.Get();

    context_->CSSetShader(cs_convert_yuv_.Get(), nullptr, 0);
    context_->CSSetShaderResources(0, 3, srvs);
    context_->CSSetUnorderedAccessViews(0, 1, &uav, nullptr);
    context_->CSSetConstantBuffers(0, 1, &cb);
    context_->CSSetSamplers(0, 1, &sampler);
    context_->Dispatch(DivRoundUp(frame->src_width, 8), DivRoundUp(frame->src_height, 8), 1);

    // Unbound afterwards. A view left bound as a UAV cannot then be bound as
    // an SRV for the next pass, and D3D11 reports that by silently binding
    // null -- a black picture with no error anywhere.
    ID3D11UnorderedAccessView* none_uav = nullptr;
    ID3D11ShaderResourceView* none_srv[3] = {nullptr, nullptr, nullptr};
    context_->CSSetUnorderedAccessViews(0, 1, &none_uav, nullptr);
    context_->CSSetShaderResources(0, 3, none_srv);
    return RBGC_OK;
}


// ----------------------------------------------------- the video processor

rbgc_status D3D11Renderer::EnsureVideoProcessor(int32_t in_w, int32_t in_h,
                                                int32_t out_w, int32_t out_h)
{
    if (video_processor_ && in_w == vp_in_width_ && in_h == vp_in_height_
        && out_w == vp_out_width_ && out_h == vp_out_height_)
        return RBGC_OK;

    video_processor_.Reset();
    video_enumerator_.Reset();

    if (!video_device_ && FAILED(device_.As(&video_device_)))
    {
        Fail(RBGC_ERR_UNSUPPORTED, "this graphics device has no video processor");
        return RBGC_ERR_UNSUPPORTED;
    }
    if (!video_context_ && FAILED(context_.As(&video_context_)))
    {
        Fail(RBGC_ERR_UNSUPPORTED, "this graphics device has no video context");
        return RBGC_ERR_UNSUPPORTED;
    }

    D3D11_VIDEO_PROCESSOR_CONTENT_DESC desc{};
    desc.InputFrameFormat = D3D11_VIDEO_FRAME_FORMAT_PROGRESSIVE;
    desc.InputWidth = static_cast<UINT>(std::max(in_w, 1));
    desc.InputHeight = static_cast<UINT>(std::max(in_h, 1));
    desc.OutputWidth = static_cast<UINT>(std::max(out_w, 1));
    desc.OutputHeight = static_cast<UINT>(std::max(out_h, 1));
    desc.Usage = D3D11_VIDEO_USAGE_PLAYBACK_NORMAL;

    HRESULT hr = video_device_->CreateVideoProcessorEnumerator(&desc, &video_enumerator_);
    if (SUCCEEDED(hr))
        hr = video_device_->CreateVideoProcessor(video_enumerator_.Get(), 0, &video_processor_);
    if (FAILED(hr))
    {
        Fail(RBGC_ERR_RESOURCE, "could not create a video processor", hr);
        return RBGC_ERR_RESOURCE;
    }

    // Progressive, no frame-rate conversion, no automatic anything. A driver
    // that helpfully deinterlaces or interpolates would be adding latency and
    // changing the picture behind our back -- and on a game stream the
    // interpolation would be visible as smearing during motion.
    video_context_->VideoProcessorSetStreamFrameFormat(
        video_processor_.Get(), 0, D3D11_VIDEO_FRAME_FORMAT_PROGRESSIVE);
    video_context_->VideoProcessorSetStreamAutoProcessingMode(
        video_processor_.Get(), 0, FALSE);
    video_context_->VideoProcessorSetStreamOutputRate(
        video_processor_.Get(), 0, D3D11_VIDEO_PROCESSOR_OUTPUT_RATE_NORMAL, FALSE, nullptr);

    vp_in_width_ = in_w;
    vp_in_height_ = in_h;
    vp_out_width_ = out_w;
    vp_out_height_ = out_h;
    vsr_accepted_ = false;
    vsr_requested_ = false;
    return RBGC_OK;
}

rbgc_status D3D11Renderer::BltHardware(const rbgc_frame* frame,
                                       ID3D11Texture2D* destination,
                                       const RECT& src_rect, const RECT& dst_rect,
                                       bool want_super_resolution)
{
    const int32_t in_w = static_cast<int32_t>(src_rect.right - src_rect.left);
    const int32_t in_h = static_cast<int32_t>(src_rect.bottom - src_rect.top);
    const int32_t out_w = static_cast<int32_t>(dst_rect.right - dst_rect.left);
    const int32_t out_h = static_cast<int32_t>(dst_rect.bottom - dst_rect.top);

    rbgc_status status = EnsureVideoProcessor(in_w, in_h, out_w, out_h);
    if (status != RBGC_OK)
        return status;

    ID3D11Texture2D* source = nullptr;
    UINT array_slice = 0;
    if (frame->hw_texture)
    {
        source = static_cast<ID3D11Texture2D*>(frame->hw_texture);
        array_slice = frame->hw_slice;
    }
    else
    {
        source = nv12_vp_tex_.Get();
    }
    if (!source)
    {
        Fail(RBGC_ERR_ARG, "no video source for the video processor");
        return RBGC_ERR_ARG;
    }

    D3D11_VIDEO_PROCESSOR_INPUT_VIEW_DESC in_desc{};
    in_desc.FourCC = 0;
    in_desc.ViewDimension = D3D11_VPIV_DIMENSION_TEXTURE2D;
    in_desc.Texture2D.MipSlice = 0;
    in_desc.Texture2D.ArraySlice = array_slice;

    ComPtr<ID3D11VideoProcessorInputView> input_view;
    HRESULT hr = video_device_->CreateVideoProcessorInputView(
        source, video_enumerator_.Get(), &in_desc, &input_view);
    if (FAILED(hr))
    {
        Fail(RBGC_ERR_RESOURCE, "could not view the decoder's texture", hr);
        return RBGC_ERR_RESOURCE;
    }

    D3D11_VIDEO_PROCESSOR_OUTPUT_VIEW_DESC out_desc{};
    out_desc.ViewDimension = D3D11_VPOV_DIMENSION_TEXTURE2D;
    out_desc.Texture2D.MipSlice = 0;

    ComPtr<ID3D11VideoProcessorOutputView> output_view;
    hr = video_device_->CreateVideoProcessorOutputView(
        destination, video_enumerator_.Get(), &out_desc, &output_view);
    if (FAILED(hr))
    {
        Fail(RBGC_ERR_RESOURCE, "could not view the destination", hr);
        return RBGC_ERR_RESOURCE;
    }

    // THE CROP. The upscaler never sees a pixel outside this rectangle,
    // because the video processor is what reads the decoder's texture and it
    // is told to read only this much of it.
    video_context_->VideoProcessorSetStreamSourceRect(
        video_processor_.Get(), 0, TRUE, &src_rect);
    video_context_->VideoProcessorSetStreamDestRect(
        video_processor_.Get(), 0, TRUE, &dst_rect);
    video_context_->VideoProcessorSetOutputTargetRect(
        video_processor_.Get(), TRUE, &dst_rect);

    // Colour, told rather than guessed. A wrong matrix here is the "FSR looks
    // washed out" report, and nothing about the picture says which end got it
    // wrong.
    D3D11_VIDEO_PROCESSOR_COLOR_SPACE colour{};
    colour.Usage = 0;      // playback, not video processing
    colour.RGB_Range = 0;  // full-range RGB out
    colour.YCbCr_Matrix = MatrixFor(frame->colorspace, frame->src_height) == 0 ? 0 : 1;
    colour.YCbCr_xvYCC = 0;
    colour.Nominal_Range = FullRangeFor(frame->color_range)
                               ? D3D11_VIDEO_PROCESSOR_NOMINAL_RANGE_0_255
                               : D3D11_VIDEO_PROCESSOR_NOMINAL_RANGE_16_235;
    video_context_->VideoProcessorSetStreamColorSpace(video_processor_.Get(), 0, &colour);

    D3D11_VIDEO_PROCESSOR_COLOR_SPACE out_colour = colour;
    out_colour.Nominal_Range = D3D11_VIDEO_PROCESSOR_NOMINAL_RANGE_0_255;
    video_context_->VideoProcessorSetOutputColorSpace(video_processor_.Get(), &out_colour);

    // RTX Video Super Resolution. Enabling it is one call and there is no API
    // that reports whether the driver then actually ran it -- which is why
    // nothing anywhere in this feature claims it is "active" rather than
    // "requested".
    if (want_super_resolution != vsr_requested_ || !vsr_accepted_)
    {
        NvidiaStreamExtension ext{
            kNvidiaPPEVersion, kNvidiaPPESuperResolution,
            want_super_resolution ? 1u : 0u};
        HRESULT ext_hr = video_context_->VideoProcessorSetStreamExtension(
            video_processor_.Get(), 0, &kNvidiaPPEInterfaceGUID,
            static_cast<UINT>(sizeof(ext)), &ext);
        vsr_accepted_ = SUCCEEDED(ext_hr);
        vsr_requested_ = want_super_resolution;
    }

    D3D11_VIDEO_PROCESSOR_STREAM stream{};
    stream.Enable = TRUE;
    stream.OutputIndex = 0;
    stream.InputFrameOrField = 0;
    stream.pInputSurface = input_view.Get();

    hr = video_context_->VideoProcessorBlt(
        video_processor_.Get(), output_view.Get(), 0, 1, &stream);
    if (FAILED(hr))
    {
        const rbgc_status fault = hr == DXGI_ERROR_DEVICE_REMOVED
                                      ? RBGC_ERR_DEVICE_LOST
                                      : RBGC_ERR_RESOURCE;
        Fail(fault, "the video processor refused the frame", hr);
        return fault;
    }
    return RBGC_OK;
}
