import { useEffect, useState } from 'react'
import { localize } from '../app/i18n'
import { IconArrowRight, IconBatch, IconCheck, IconDownload, IconFilm, IconGlobe, IconLayers, IconLogo, IconMic, IconPlay } from '../shared/components/Icons'
import './LandingPage.css'

type DownloadKey = 'windows' | 'macos-arm64' | 'macos-x64'
type DesktopDownloads = { version: string; downloads: Partial<Record<DownloadKey, { url: string; name: string }>> }

export default function LandingPage() {
  const [locale, setLocale] = useState<'vi' | 'en'>('vi')
  const [theme, setTheme] = useState<'light' | 'dark'>(() => {
    try { return localStorage.getItem('zm-landing-theme') === 'dark' ? 'dark' : 'light' } catch { return 'light' }
  })
  const [preview, setPreview] = useState(0)
  const [macArch, setMacArch] = useState<'macos-arm64' | 'macos-x64'>('macos-arm64')
  const [release, setRelease] = useState<DesktopDownloads | null>(null)
  const [downloadState, setDownloadState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [retry, setRetry] = useState(0)
  const t = (vi: string, en: string) => localize(locale, vi, en)

  useEffect(() => {
    const nodes = [document.documentElement, document.body, document.getElementById('root')].filter(Boolean) as HTMLElement[]
    const previous = nodes.map(node => ({ node, overflow: node.style.overflow, height: node.style.height, minHeight: node.style.minHeight }))
    nodes.forEach(node => { node.style.overflow = 'auto'; node.style.height = 'auto'; node.style.minHeight = '100%' })
    return () => previous.forEach(({ node, overflow, height, minHeight }) => { node.style.overflow = overflow; node.style.height = height; node.style.minHeight = minHeight })
  }, [])
  useEffect(() => {
    try { localStorage.setItem('zm-landing-theme', theme) } catch { /* Storage can be unavailable. */ }
  }, [theme])
  useEffect(() => {
    const controller = new AbortController()
    let active = true
    const timeout = window.setTimeout(() => controller.abort(), 30_000)
    setDownloadState('loading')
    fetch('/api/system/downloads', { signal: controller.signal })
      .then(async response => {
        if (!response.ok) throw new Error('DOWNLOAD_RELEASE_UNAVAILABLE')
        const data: DesktopDownloads = await response.json()
        if (!data.downloads || typeof data.version !== 'string') throw new Error('DOWNLOAD_RELEASE_INVALID')
        if (active) { setRelease(data); setDownloadState('ready') }
      })
      .catch(() => { if (active) setDownloadState('error') })
      .finally(() => window.clearTimeout(timeout))
    return () => { active = false; window.clearTimeout(timeout); controller.abort() }
  }, [retry])

  const previews = [
    { title: t('Live Preview Editor', 'Live Preview Editor'), image: '/previews/live-previews.png', icon: IconLayers },
    { title: t('Clone & dịch video', 'Clone & translate video'), image: '/previews/clone-video.png', icon: IconGlobe },
    { title: t('Flow tạo video', 'Video creation Flow'), image: '/previews/flow.png', icon: IconFilm },
    { title: t('TTS Studio', 'TTS Studio'), image: '/previews/text-to-speed.png', icon: IconMic },
  ]
  const products = [
    { title: t('Dịch video. Giữ trọn câu chuyện.', 'Translate video. Keep the story.'), text: t('Nhận dạng lời thoại, OCR, dịch theo ngữ cảnh và lồng tiếng. Chỉnh từng câu, từng giọng ngay trong một quy trình.', 'Transcribe speech, read on-screen text, translate in context and dub. Fine-tune every line and voice in one workflow.'), image: '/previews/clone-video.png', path: '/video-clone', icon: IconGlobe, tag: t('CLONE & DỊCH VIDEO', 'CLONE & TRANSLATE'), featured: true },
    { title: t('Từ kịch bản đến cảnh quay.', 'From script to scene.'), text: t('Tạo storyboard, keyframe và chuỗi video với Flow. Duyệt ý tưởng trước khi dựng thành phẩm.', 'Create storyboards, keyframes and video sequences with Flow. Review your ideas before the final render.'), image: '/previews/flow.png', path: '/flow-veo', icon: IconFilm, tag: t('FLOW TẠO VIDEO', 'VIDEO CREATION FLOW'), featured: true },
    { title: t('Giọng đọc có chất riêng.', 'A voice with character.'), text: t('Tạo lời đọc AI, chọn giọng cho từng vai và nghe thử trước khi sử dụng.', 'Generate AI voiceovers, cast voices for each role and listen before you use them.'), image: '/previews/text-to-speed.png', path: '/text-to-speech', icon: IconMic, tag: t('GIỌNG ĐỌC AI', 'AI VOICEOVER') },
    { title: t('Mọi chi tiết, trên timeline.', 'Every detail, on a timeline.'), text: t('Chỉnh timing, phụ đề, âm thanh và bố cục với bản xem trước trực quan.', 'Adjust timing, subtitles, audio and layout with a visual preview.'), image: '/previews/live-previews.png', path: '/live-preview', icon: IconLayers, tag: t('LIVE PREVIEW EDITOR', 'LIVE PREVIEW EDITOR') },
    { title: t('Phụ đề theo phong cách bạn.', 'Subtitles in your style.'), text: t('Tùy chỉnh font, màu sắc, vị trí và xuất phụ đề cho video của bạn.', 'Customize fonts, colors and placement, then export subtitles for your video.'), image: '/previews/caption.png', path: '/subtitle-export', icon: IconGlobe, tag: t('PHỤ ĐỀ & CAPTION', 'SUBTITLES & CAPTIONS') },
    { title: t('Khung hình gọn, sạch hơn.', 'A cleaner frame.'), text: t('Xử lý vùng chữ, logo và chi tiết thừa trước khi dựng lại video.', 'Process text, logos and unwanted details before editing your video.'), image: '/previews/clean-video.png', path: '/video-cleaner', icon: IconFilm, tag: t('VIDEO CLEANER', 'VIDEO CLEANER') },
  ]
  const outcomes = [
    { icon: IconGlobe, tag: t('VIDEO CÓ SẴN', 'AN EXISTING VIDEO'), title: t('Một câu chuyện. Thêm ngôn ngữ.', 'One story. More languages.'), text: t('Đưa video đến khán giả mới với bản dịch, giọng lồng tiếng và phụ đề bạn có thể kiểm tra, chỉnh sửa.', 'Bring your video to a new audience with translations, dubbing and subtitles you can review and edit.'), steps: t('Nhận dạng → Dịch → Lồng tiếng', 'Transcribe → Translate → Dub'), path: products[0].path },
    { icon: IconMic, tag: t('KỊCH BẢN CỦA BẠN', 'YOUR SCRIPT'), title: t('Kịch bản. Thành giọng kể.', 'Your script. A new voice.'), text: t('Chọn giọng, nghe thử từng câu và chỉnh timing để lời đọc đi cùng nhịp video.', 'Choose a voice, preview each line and adjust the timing to match your video.'), steps: t('Kịch bản → Giọng đọc → Timing', 'Script → Voice → Timing'), path: products[2].path },
    { icon: IconFilm, tag: t('Ý TƯỞNG MỚI', 'A NEW IDEA'), title: t('Từ một prompt. Đến cảnh quay.', 'From a prompt. To a scene.'), text: t('Phát triển ý tưởng thành storyboard, keyframe và cảnh quay với Flow, rồi hoàn thiện trong editor.', 'Develop your idea into storyboards, keyframes and scenes with Flow, then finish it in the editor.'), steps: t('Ý tưởng → Flow → Biên tập', 'Idea → Flow → Edit'), path: products[1].path },
  ]
  const workflow = [
    [t('Đưa ý tưởng vào', 'Bring your idea'), t('Bắt đầu từ video, SRT, văn bản hoặc một prompt.', 'Start with a video, SRT, text or a prompt.')],
    [t('Để AI hỗ trợ', 'Let AI help'), t('Nhận dạng, dịch, tạo giọng hoặc dựng cảnh theo nhu cầu.', 'Transcribe, translate, generate voices or build scenes.')],
    [t('Chỉnh theo ý bạn', 'Make it yours'), t('Duyệt nội dung, chỉnh giọng, phụ đề và timeline.', 'Review the content, voices, subtitles and timeline.')],
    [t('Xuất & chia sẻ', 'Export & share'), t('Theo dõi tác vụ và quản lý thành phẩm trong workspace.', 'Track tasks and manage finished work in your workspace.')],
  ]
  const guides = [
    { title: t('Cài đặt ứng dụng', 'Install the app'), intro: t('Chọn bộ cài cho Windows hoặc macOS và mở ZM AI TOOL.', 'Choose the Windows or macOS installer and open ZM AI TOOL.'), steps: [
      t('Tải bộ cài ở mục Tải về. Với macOS, chọn Apple Silicon hoặc Intel đúng với máy của bạn.', 'Download an installer from the Download section. On macOS, choose Apple Silicon or Intel to match your Mac.'),
      t('Windows: mở file Setup.exe và làm theo trình cài đặt. macOS: mở file .pkg và làm theo các bước cài đặt.', 'Windows: open Setup.exe and follow the installer. macOS: open the .pkg file and follow the installation steps.'),
      t('Mở ZM AI TOOL sau khi cài xong. Khi ứng dụng yêu cầu bản quyền, nhập key của bạn và nhấn Kích hoạt.', 'Open ZM AI TOOL after installation. When the app requests a license, enter your key and click Activate.'),
    ] },
    { title: t('Cấu hình lần đầu', 'First-time setup'), intro: t('Chuẩn bị công cụ xử lý và kết nối AI bạn muốn dùng.', 'Prepare processing tools and connect the AI services you want to use.'), steps: [
      t('Ở lần mở đầu tiên, hoàn tất màn hình Thiết lập. Bạn có thể kiểm tra lại tại Cấu hình → Thiết lập.', 'On first launch, complete the setup screen. You can check it again under Settings → Settings.'),
      t('Kiểm tra FFmpeg và các tài nguyên cần cho công việc. Cài model nhận dạng, OCR hoặc giọng đọc khi sử dụng tính năng tương ứng.', 'Check FFmpeg and the resources needed for your work. Install speech recognition, OCR or voice models when using those features.'),
      t('Nếu dùng dịch vụ cloud, vào Cấu hình → Cloud AI, chọn nhà cung cấp, nhập API key và lưu. Flow cần kết nối tài khoản Google trong workspace Flow.', 'For cloud services, go to Settings → Cloud AI, select a provider, enter your API key and save. Flow requires connecting a Google account in the Flow workspace.'),
    ] },
    { title: t('Dịch & lồng tiếng video', 'Translate & dub a video'), intro: t('Tạo project từ video, nhận dạng nội dung và chọn giọng đọc.', 'Create a project from a video, recognize its content and choose voices.'), steps: [
      t('Mở Clone / Review và đưa video vào project. Chọn Whisper, CapCut, OCR hoặc SRT tùy nguồn nội dung.', 'Open Clone / Review and add a video to your project. Choose Whisper, CapCut, OCR or SRT according to your content source.'),
      t('Chọn ngôn ngữ nguồn, ngôn ngữ đích và công cụ dịch. Chạy nhận dạng, kiểm tra các câu rồi dịch nội dung.', 'Select source and target languages and a translation provider. Run recognition, check the lines, then translate the content.'),
      t('Chọn giọng cho từng vai, kiểm tra bản dịch và tạo lồng tiếng. Mở bản xem trước để nghe và kiểm tra timing trước khi xuất.', 'Choose a voice for each role, review the translation and generate dubbing. Open the preview to listen and check timing before exporting.'),
    ] },
    { title: t('Chỉnh sửa & xuất bản', 'Edit & export'), intro: t('Hoàn thiện hình, phụ đề và âm thanh trong Live Preview Editor.', 'Finish visuals, subtitles and audio in Live Preview Editor.'), steps: [
      t('Mở Xem trước & sửa từ project. Chọn đoạn trên timeline để chỉnh nội dung, thời gian và giọng đọc.', 'Open Preview & edit from your project. Select a timeline segment to adjust its content, timing and voice.'),
      t('Chỉnh font, màu, vị trí caption; xử lý vùng che chữ cũ và cân bằng âm gốc với lồng tiếng. Thêm media, logo hoặc overlay khi cần.', 'Adjust caption fonts, colors and placement; cover old on-screen text and balance original audio with dubbing. Add media, logos or overlays as needed.'),
      t('Chạy Preview cho một đoạn ngắn để kiểm tra kết quả. Khi đã hài lòng, chọn Xuất bản và theo dõi tác vụ đến khi hoàn tất.', 'Run Preview on a short segment to check the result. When satisfied, choose Publish and follow the task until it finishes.'),
    ] },
  ]
  const faqs = [
    [t('ZM AI TOOL dành cho ai?', 'Who is ZM AI TOOL for?'), t('Nhà sáng tạo nội dung, người làm video dịch, lồng tiếng, review và đội sản xuất cần nhiều công cụ trong một workspace.', 'Creators, video translators, voiceover artists, reviewers and production teams who need their tools in one workspace.')],
    [t('Tôi nên tải bản macOS nào?', 'Which macOS version should I download?'), t('Chọn Apple Silicon nếu Mac dùng chip M1, M2, M3, M4 hoặc mới hơn. Chọn Intel nếu Mac dùng bộ xử lý Intel. Chỉ những bộ cài đã phát hành mới có thể tải.', 'Choose Apple Silicon for a Mac with an M1, M2, M3, M4 or newer chip. Choose Intel for a Mac with an Intel processor. Only published installers are available to download.')],
    [t('Các tính năng AI có cần mạng không?', 'Do AI features require internet access?'), t('Tùy công cụ và model bạn chọn. Model local chạy trên máy sau khi tải đủ dữ liệu; Flow và các nhà cung cấp API cần kết nối mạng, tài khoản hoặc API key phù hợp.', 'It depends on the tool and model. Local models run on your machine after their files are downloaded; Flow and API providers require internet access and the appropriate account or API key.')],
  ]
  const downloadLink = (key: DownloadKey, label: string, className = 'lp-button lp-button-primary') => {
    const asset = downloadState === 'ready' ? release?.downloads[key] : undefined
    return asset
      ? <a className={className} href={asset.url} download={asset.name}><IconDownload size={18} />{label}<IconArrowRight size={17} /></a>
      : <button type="button" className={className} disabled><IconDownload size={18} />{downloadState === 'loading' ? t('Đang lấy bản tải…', 'Loading downloads…') : downloadState === 'error' ? t('Chưa lấy được bản tải', 'Downloads unavailable') : t('Chưa có bản tải', 'Not yet available')}</button>
  }

  return <div className="landing" data-theme={theme} lang={locale}>
    <a className="lp-skip" href="#main">{t('Đến nội dung chính', 'Skip to content')}</a>
    <header className="lp-nav">
      <div className="lp-container lp-nav-inner">
        <a className="lp-brand" href="/"><IconLogo size={34} /><span>ZM AI TOOL</span></a>
        <nav className="lp-nav-links" aria-label={t('Điều hướng chính', 'Main navigation')}>
          <a href="#product">{t('Công cụ AI', 'AI tools')}</a><a href="#workflow">{t('Quy trình', 'Workflow')}</a><a href="#guide">{t('Hướng dẫn', 'Guide')}</a><a href="#faq">{t('Hỏi đáp', 'FAQ')}</a>
        </nav>
        <div className="lp-nav-actions">
          <button className="lp-icon-button" type="button" onClick={() => setTheme(theme === 'light' ? 'dark' : 'light')} aria-label={theme === 'light' ? t('Chuyển sang giao diện tối', 'Switch to dark theme') : t('Chuyển sang giao diện sáng', 'Switch to light theme')}><span aria-hidden="true">{theme === 'light' ? '☾' : '☼'}</span></button>
          <button className="lp-locale" type="button" onClick={() => setLocale(locale === 'vi' ? 'en' : 'vi')} aria-label={t('Switch to English', 'Chuyển sang tiếng Việt')}>{locale === 'vi' ? 'EN' : 'VI'}</button>
          <a className="lp-workspace-link" href="/video-clone">{t('Mở workspace', 'Open workspace')}<IconArrowRight size={16} /></a>
          <a className="lp-button lp-button-primary lp-nav-download" href="#download"><IconDownload size={16} />{t('Tải về', 'Download')}</a>
        </div>
      </div>
    </header>

    <main id="main">
      <section className="lp-hero">
        <div className="lp-container">
          <div className="lp-hero-top">
          <div className="lp-hero-copy">
            <div className="lp-eyebrow lp-hero-badge"><span className="lp-status-dot" />{t('STUDIO AI CHO NHÀ SÁNG TẠO', 'AI STUDIO FOR CREATORS')}<span className="lp-badge-tag">Windows + macOS</span></div>
            <h1>{t('Làm video.', 'Make videos.')}<br /><span>{t('Đậm chất của bạn.', 'Make them yours.')}</span></h1>
            <div className="lp-hero-signature"><span aria-hidden="true">✦</span>{t('AI xử lý. Bạn làm chủ câu chuyện.', 'AI does the processing. You own the story.')}</div>
          </div>
          <div className="lp-hero-pitch">
          <p className="lp-hero-intro">{t('Dịch & lồng tiếng video. Tạo giọng đọc. Dựng cảnh với Flow.', 'Translate & dub videos. Generate voices. Create scenes with Flow.')} <strong>{t('Từ ý tưởng đến timeline — trong một ứng dụng.', 'From your idea to the timeline — in one app.')}</strong></p>
          <div className="lp-hero-actions">
            {downloadLink('windows', t('Tải cho Windows', 'Download for Windows'))}
            {downloadLink(macArch, t('Tải cho macOS', 'Download for macOS'), 'lp-button lp-button-secondary')}
            <a className="lp-text-link" href="#guide"><IconPlay size={16} />{t('Xem hướng dẫn', 'View guide')}</a>
          </div>
          <div className="lp-hero-meta"><span><IconCheck size={15} />{t('Chỉnh trực tiếp trên timeline', 'Edit directly on the timeline')}</span><a href="#download">macOS: {macArch === 'macos-arm64' ? 'Apple Silicon' : 'Intel'} · {t('Chọn bản khác', 'Choose another version')}<IconArrowRight size={13} /></a></div>
          {downloadState === 'error' && <p className="lp-download-error" role="status">{t('Chưa kết nối được nguồn tải.', 'Could not connect to the download source.')} <button type="button" onClick={() => setRetry(value => value + 1)}>{t('Thử lại', 'Retry')}</button></p>}
          </div>
          </div>
          <div className="lp-preview-stage">
          <div className="lp-studio-caption"><span><IconLayers size={16} />{t('KHÔNG GIAN SÁNG TẠO CỦA BẠN', 'YOUR CREATIVE SPACE')}</span><span>{t('Giao diện thật. Quyền kiểm soát thật.', 'Real interface. Real control.')}<IconArrowRight size={16} /></span></div>
          <div className="lp-preview">
            <div className="lp-preview-bar"><span className="lp-window-dots" aria-hidden="true"><i /><i /><i /></span><span>ZM AI TOOL <span className="lp-preview-bar-divider">/</span> {previews[preview].title}</span><span className="lp-preview-label">{t('GIAO DIỆN ỨNG DỤNG', 'APP PREVIEW')}</span></div>
            <div className="lp-preview-screen" id="workspace-preview" role="tabpanel" aria-labelledby={'preview-tab-' + preview}><img src={previews[preview].image} alt={previews[preview].title} fetchPriority="high" /></div>
            <div className="lp-preview-tabs" role="tablist" aria-label={t('Xem các workspace', 'Preview workspaces')}>
              {previews.map((item, index) => <button key={item.image} id={'preview-tab-' + index} type="button" role="tab" aria-selected={preview === index} aria-controls="workspace-preview" tabIndex={preview === index ? 0 : -1} onClick={() => setPreview(index)} onKeyDown={event => {
                if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
                event.preventDefault()
                const next = event.key === 'Home' ? 0 : event.key === 'End' ? previews.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + previews.length) % previews.length
                setPreview(next)
                document.getElementById('preview-tab-' + next)?.focus()
              }}><item.icon size={17} />{item.title}</button>)}
            </div>
          </div>
          <div className="lp-preview-note"><span><span className="lp-status-dot" />{t('Xem trước. Chỉnh lại. Rồi xuất bản.', 'Preview. Refine. Then publish.')}</span><span>{t('Bốn workspace · Một quy trình liền mạch', 'Four workspaces · One connected workflow')}</span></div>
          </div>
        </div>
      </section>

      <div className="lp-capabilities lp-container"><span><IconGlobe size={19} />{t('Dịch theo ngữ cảnh', 'Contextual translation')}</span><span><IconMic size={19} />{t('Giọng đọc AI', 'AI voices')}</span><span><IconFilm size={19} />{t('Tạo video với Flow', 'Create videos with Flow')}</span><span><IconBatch size={19} />{t('Xử lý hàng loạt', 'Batch processing')}</span></div>

      <section className="lp-section lp-container lp-outcomes-section" id="use-cases">
        <div className="lp-section-heading lp-heading-split"><div><div className="lp-eyebrow">{t('BẠN MUỐN TẠO ĐIỀU GÌ?', 'WHAT WILL YOU CREATE?')}</div><h2>{t('Bắt đầu từ thứ bạn có.', 'Start with what you have.')}<br /><span>{t('Làm nên thứ bạn muốn.', 'Make what you imagine.')}</span></h2></div><p>{t('Một video cần dịch, một kịch bản cần giọng kể, hay một ý tưởng cần thành hình. Có một lối đi cho bạn.', 'A video to translate, a script that needs a voice, or an idea waiting to take shape. There is a path for you.')}</p></div>
        <div className="lp-outcomes-grid">{outcomes.map((item, index) => <article className="lp-outcome-card" key={item.path}>
          <div className="lp-outcome-top"><item.icon size={25} /><span>0{index + 1}</span></div><span className="lp-outcome-tag">{item.tag}</span><h3>{item.title}</h3><p>{item.text}</p><div className="lp-outcome-bottom"><span>{item.steps}</span><a href={item.path} aria-label={t('Khám phá: ', 'Explore: ') + item.title}><IconArrowRight size={21} /></a></div>
        </article>)}</div>
      </section>

      <section className="lp-section lp-container" id="product">
        <div className="lp-section-heading lp-heading-split"><div><div className="lp-eyebrow">{t('BỘ CÔNG CỤ CỦA BẠN', 'YOUR CREATIVE TOOLKIT')}</div><h2>{t('AI làm phần nặng.', 'AI handles the heavy work.')}<br /><span>{t('Bạn giữ phần sáng tạo.', 'You keep the creative control.')}</span></h2></div><p>{t('Không dừng ở nút tự động. Duyệt từng câu dịch, chọn từng giọng và chỉnh từng chi tiết trước khi xuất.', 'Go beyond an automatic result. Review every translation, choose every voice and refine each detail before exporting.')}</p></div>
        <div className="lp-product-grid">{products.map(item => <article className={'lp-product-card' + (item.featured ? ' lp-product-featured' : '')} key={item.path}>
          <div className="lp-product-copy"><span className="lp-product-tag"><item.icon size={16} />{item.tag}</span><h3>{item.title}</h3><p>{item.text}</p><a className="lp-text-link" href={item.path}>{t('Mở công cụ', 'Open tool')}<IconArrowRight size={17} /></a></div>
          <div className="lp-product-image"><img src={item.image} alt={item.tag} loading="lazy" /></div>
        </article>)}</div>
      </section>

      <section className="lp-workflow-section" id="workflow"><div className="lp-container lp-section">
        <div className="lp-section-heading"><div className="lp-eyebrow">{t('TỪ Ý TƯỞNG ĐẾN THÀNH PHẨM', 'FROM IDEA TO FINISHED VIDEO')}</div><h2>{t('Đừng để ý tưởng mắc kẹt', 'Do not let your idea get stuck')}<br /><span>{t('giữa quá nhiều công cụ.', 'between too many tools.')}</span></h2><p>{t('Đưa nội dung vào. Để AI hỗ trợ. Tự tay hoàn thiện. Một luồng làm việc, từ đầu đến cuối.', 'Bring in your content. Let AI help. Make it yours. One workflow, from start to finish.')}</p></div>
        <div className="lp-workflow">{workflow.map(([title, text], index) => <article key={title}><span className="lp-step-number">0{index + 1}</span><h3>{title}</h3><p>{text}</p></article>)}</div>
        <a className="lp-button lp-button-secondary" href="/video-clone">{t('Khám phá workspace', 'Explore the workspace')}<IconArrowRight size={17} /></a>
      </div></section>

      <section className="lp-section lp-container lp-guide-section" id="guide">
        <div className="lp-section-heading"><div className="lp-eyebrow">{t('HƯỚNG DẪN SỬ DỤNG', 'USER GUIDE')}</div><h2>{t('Bắt đầu từng bước.', 'Start step by step.')}<br /><span>{t('Tự tin tạo video đầu tiên.', 'Create your first video with confidence.')}</span></h2><p>{t('Từ cài đặt, cấu hình đến chỉnh sửa và xuất video — đầy đủ các bước bên dưới.', 'From installation and setup to editing and export — all the steps below.')}</p></div>
        <div className="lp-guide-grid">{guides.map((guide, index) => <details className="lp-guide-card" key={guide.title} open>
          <summary className="lp-guide-header"><span className="lp-step-number">0{index + 1}</span><h3>{guide.title}</h3><span className="lp-guide-toggle" aria-hidden="true">+</span></summary>
          <p className="lp-guide-intro">{guide.intro}</p>
          <ol>{guide.steps.map(step => <li key={step}>{step}</li>)}</ol>
          {index === 0 && <a className="lp-text-link" href="#download">{t('Đến phần tải về', 'Go to downloads')}<IconArrowRight size={16} /></a>}
        </details>)}</div>
      </section>

      <section className="lp-section lp-container" id="download">
        <div className="lp-download-heading"><div><div className="lp-eyebrow">{t('ĐẾN LƯỢT BẠN SÁNG TẠO', 'YOUR TURN TO CREATE')}</div><h2>{t('Video tiếp theo.', 'Your next video.')}<br /><span>{t('Bắt đầu ngay tại đây.', 'Starts right here.')}</span></h2></div><p>{t('Cài ZM AI TOOL, mở project đầu tiên và đưa ý tưởng của bạn lên timeline.', 'Install ZM AI TOOL, open your first project and bring your idea to the timeline.')}<span className="lp-release-version">{downloadState === 'ready' && release?.version ? t('Phiên bản mới nhất', 'Latest version') + ' · v' + release.version : t('Windows & macOS', 'Windows & macOS')}</span></p></div>
        <div className="lp-download-grid">
          <article className="lp-download-card"><div className="lp-os-icon" aria-hidden="true"><svg viewBox="0 0 24 24" width="27" height="27" fill="currentColor"><path d="M2 4.5 11 3v8H2zm10-1.7L22 1v10H12zM2 12h9v8l-9-1.5zm10 0h10v10l-10-1.8z" /></svg></div><h3>Windows</h3><p>{t('Bộ cài cho máy tính Windows 64-bit.', 'Installer for 64-bit Windows computers.')}</p><span className="lp-os-detail">x64 · {t('Bộ cài Setup', 'Setup installer')}</span>{downloadLink('windows', t('Tải cho Windows', 'Download for Windows'))}</article>
          <article className="lp-download-card"><div className="lp-os-icon" aria-hidden="true"><svg viewBox="0 0 24 24" width="29" height="29" fill="none" stroke="currentColor" strokeWidth="1.7"><rect x="3" y="3" width="18" height="13" rx="2" /><path d="M9 21h6m-3-5v5" /></svg></div><h3>macOS</h3><p>{t('Chọn đúng bộ xử lý của máy Mac.', 'Choose the processor in your Mac.')}</p><label className="lp-mac-choice"><span>{t('Bộ xử lý', 'Processor')}</span><select value={macArch} onChange={event => setMacArch(event.target.value as typeof macArch)}><option value="macos-arm64">Apple Silicon (M1+)</option><option value="macos-x64">Intel (x64)</option></select></label>{downloadLink(macArch, t('Tải cho macOS', 'Download for macOS'))}</article>
        </div>
        <div className="lp-download-note" role="status">{downloadState === 'loading' ? t('Đang kiểm tra các bộ cài mới nhất…', 'Checking the latest installers…') : downloadState === 'error' ? <>{t('Không lấy được danh sách bộ cài.', 'Could not load installers.')} <button type="button" onClick={() => setRetry(value => value + 1)}>{t('Thử lại', 'Retry')}</button></> : t('Nhấn nút để tải trực tiếp bộ cài đã phát hành. Bản chưa phát hành sẽ được hiển thị khi có sẵn.', 'Click to download a published installer directly. Unreleased versions will appear when available.')}</div>
      </section>

      <section className="lp-section lp-container lp-faq" id="faq"><div><div className="lp-eyebrow">{t('MỘT VÀI ĐIỀU BẠN CẦN BIẾT', 'A FEW THINGS TO KNOW')}</div><h2>{t('Bạn hỏi.', 'Your questions.')}<br /><span>{t('Chúng tôi trả lời.', 'Answered.')}</span></h2></div><div>{faqs.map(([question, answer]) => <details key={question}><summary>{question}<span aria-hidden="true">+</span></summary><p>{answer}</p></details>)}</div></section>
    </main>

    <footer className="lp-footer"><div className="lp-container"><div><a className="lp-brand" href="/"><IconLogo size={30} /><span>ZM AI TOOL</span></a><p>{t('Một workspace cho mọi nhịp sáng tạo.', 'One workspace for every creative moment.')}</p></div><nav aria-label={t('Liên kết cuối trang', 'Footer links')}><a href="#product">{t('Công cụ AI', 'AI tools')}</a><a href="#guide">{t('Hướng dẫn', 'Guide')}</a><a href="#download">{t('Tải về', 'Download')}</a><a href="#faq">{t('Hỏi đáp', 'FAQ')}</a><a href="/video-clone">{t('Mở workspace', 'Open workspace')}</a></nav><span>© {new Date().getFullYear()} ZM AI TOOL</span></div></footer>
  </div>
}
