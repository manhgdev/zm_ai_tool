import { useEffect, useState } from 'react'
import './LandingPage.css'

type Locale = 'vi' | 'en'
const copy = {
  vi: {
    nav: ['Tính năng', 'Sản phẩm', 'Quy trình'],
    badge: 'Bộ máy sáng tạo AI cho video thế hệ mới',
    title: 'Biến ý tưởng thành video\nđáng để xem lại.',
    intro: 'ZM AI TOOL gom mọi công đoạn từ viết, dịch, lồng tiếng đến dựng hình vào một workspace gọn gàng — để bạn làm nhanh hơn mà vẫn giữ chất riêng.',
    primary: 'Mở workspace', secondary: 'Xem cách hoạt động',
    trusted: 'Được tạo cho những người làm nội dung nghiêm túc',
    featureTitle: 'Một workspace.\nMọi nhịp sáng tạo.',
    featureIntro: 'Không còn những tab rời rạc và file thất lạc. ZM AI TOOL giữ toàn bộ ý tưởng, media và output trong một luồng làm việc mạch lạc.',
    features: [['01', 'Dịch & lồng tiếng', 'Giữ đúng nhịp câu chuyện với giọng AI tự nhiên, đồng bộ theo timeline.'], ['02', 'Tạo hình & video', 'Từ một prompt đến keyframe, cảnh quay và bản dựng có thể chỉnh sửa.'], ['03', 'Tự động hóa', 'Lặp lại các pipeline dài chỉ với một lần thiết lập.']],
    workflow: 'Từ bản nháp đến bản phát hành', workflowIntro: 'Bắt đầu với một ý tưởng. Kết thúc bằng một sản phẩm hoàn chỉnh, sẵn sàng xuất bản.',
    cta: 'Sẵn sàng làm điều gì đó khác biệt?', ctaText: 'Bước vào workspace của bạn và để phần còn lại cho ZM AI TOOL.', footer: 'Công cụ sáng tạo video AI cho những ý tưởng không chịu đứng yên.',
    productTitle: 'Không chỉ là AI.\nĐây là cả một studio.', productIntro: 'Sáu workspace chuyên dụng nối liền toàn bộ quy trình — từ footage thô đến video hoàn chỉnh.', productCards: [['Flow tạo video', 'Biến kịch bản thành storyboard, keyframe và chuỗi cảnh có thể duyệt trước khi render.', '/previews/flow.png', 'Khám phá Flow', '/flow-veo'], ['Clone & dịch video', 'Tách lời, dịch nội dung và dựng lại video cho thị trường mới trong một pipeline.', '/previews/clone-video.png', 'Mở Clone Video', '/'], ['TTS Studio', 'Tạo giọng đọc tự nhiên, quản lý voice và đồng bộ audio theo timeline.', '/previews/text-to-speed.png', 'Mở TTS Studio', '/text-to-speech'], ['Live Preview Editor', 'Xem, chỉnh timing, caption, audio và bố cục trước khi xuất bản cuối.', '/previews/live-previews.png', 'Mở Editor', '/live-preview'], ['Subtitle & Caption', 'Thiết kế phụ đề rõ nét, kiểm soát font, vị trí và nhịp xuất hiện theo cảnh.', '/previews/caption.png', 'Tạo phụ đề', '/subtitle-export'], ['Làm sạch video', 'Xóa watermark và chi tiết thừa, giữ lại khung hình sạch để tiếp tục sáng tạo.', '/previews/clean-video.png', 'Mở Video Cleaner', '/video-cleaner']],
  },
  en: {
    nav: ['Features', 'Product', 'Workflow'],
    badge: 'The AI creative engine for the next generation of video',
    title: 'Turn ideas into videos\nworth watching twice.',
    intro: 'ZM AI TOOL brings writing, translation, voice, visuals and editing into one focused workspace — so you move faster without losing your point of view.',
    primary: 'Open workspace', secondary: 'See how it works',
    trusted: 'Built for people who take content seriously',
    featureTitle: 'One workspace.\nEvery creative beat.',
    featureIntro: 'No more scattered tabs or lost files. ZM AI TOOL keeps every idea, asset and output in one clear creative flow.',
    features: [['01', 'Translate & dub', 'Keep the story intact with natural AI voices synced to your timeline.'], ['02', 'Generate & edit', 'Go from a prompt to keyframes, scenes and an editable cut.'], ['03', 'Automate', 'Repeat long production pipelines with one thoughtful setup.']],
    workflow: 'From rough idea to release', workflowIntro: 'Start with a spark. End with a finished piece, ready to share.',
    cta: 'Ready to make something different?', ctaText: 'Step into your workspace and let ZM AI TOOL handle the heavy lifting.', footer: 'AI video creation for ideas that refuse to sit still.',
    productTitle: 'Not just AI.\nA complete studio.', productIntro: 'Six dedicated workspaces connect the entire production flow — from raw footage to a finished video.', productCards: [['Video creation Flow', 'Turn scripts into storyboards, keyframes and scene sequences you can approve before rendering.', '/previews/flow.png', 'Explore Flow', '/flow-veo'], ['Clone & translate video', 'Extract speech, translate content and rebuild video for a new market in one pipeline.', '/previews/clone-video.png', 'Open Clone Video', '/'], ['TTS Studio', 'Create natural voiceovers, manage voices and sync audio to the timeline.', '/previews/text-to-speed.png', 'Open TTS Studio', '/text-to-speech'], ['Live Preview Editor', 'Review and adjust timing, captions, audio and layout before the final export.', '/previews/live-previews.png', 'Open Editor', '/live-preview'], ['Subtitle & Caption', 'Design readable subtitles and control font, placement and scene timing.', '/previews/caption.png', 'Create subtitles', '/subtitle-export'], ['Video Cleaner', 'Remove watermarks and unwanted details while preserving clean frames for the next edit.', '/previews/clean-video.png', 'Open Video Cleaner', '/video-cleaner']],
  },
} as const

export default function LandingPage() {
  const [locale, setLocale] = useState<Locale>('vi')
  const [theme, setTheme] = useState<'light' | 'dark'>(() => {
    try { return localStorage.getItem('zm-landing-theme') === 'dark' ? 'dark' : 'light' } catch { return 'light' }
  })
  useEffect(() => {
    const nodes = [document.documentElement, document.body, document.getElementById('root')].filter(Boolean) as HTMLElement[]
    const previous = nodes.map((node) => ({ node, overflow: node.style.overflow, height: node.style.height, minHeight: node.style.minHeight }))
    nodes.forEach((node) => { node.style.overflow = 'auto'; node.style.height = 'auto'; node.style.minHeight = '100%' })
    return () => previous.forEach(({ node, overflow, height, minHeight }) => { node.style.overflow = overflow; node.style.height = height; node.style.minHeight = minHeight })
  }, [])
  useEffect(() => {
    try { localStorage.setItem('zm-landing-theme', theme) } catch { /* localStorage can be unavailable */ }
  }, [theme])
  const t = copy[locale]
  const go = () => { window.location.href = '/text-to-speech' }
  const navTargets = ['#features', '#product', '#workflow']
  return <div className="landing" data-theme={theme}><style>{`.landing[data-theme="light"]{--muted:#3f463c}.landing[data-theme="dark"]{--muted:#c1c5bd}.landing[data-theme="light"] .feature-num,.landing[data-theme="light"] .workflow-step{color:#555c51}.landing[data-theme="dark"] .feature-num,.landing[data-theme="dark"] .workflow-step{color:#aeb3aa}.landing[data-theme="light"] .landing-marquee{color:#525950}.landing[data-theme="dark"] .landing-marquee{color:#aeb3aa}.flow-hero-shot{position:relative;width:min(520px,94%);transform:rotate(3deg);z-index:2;border:1px solid rgba(241,240,235,.28);background:#20251f;box-shadow:0 28px 70px #0008;overflow:hidden}.flow-hero-shot img{display:block;width:100%;height:auto;opacity:.92;filter:saturate(.82)}.flow-hero-shot span{position:absolute;left:16px;bottom:14px;padding:7px 9px;background:var(--acid);color:#101110;font:10px 'DM Mono';letter-spacing:.1em}`}</style>
    <nav className="landing-nav"><a className="landing-brand" href="/landing"><span className="brand-mark">Z</span><span>ZM AI TOOL</span></a><div className="landing-links">{t.nav.map((item, index) => <a href={navTargets[index]} key={item}>{item}</a>)}</div><div className="landing-nav-actions"><button className="theme-switch" type="button" onClick={() => setTheme(theme === 'light' ? 'dark' : 'light')} aria-label={locale === 'vi' ? `Chuyển sang giao diện ${theme === 'light' ? 'tối' : 'sáng'}` : `Switch to ${theme === 'light' ? 'dark' : 'light'} theme`} title={locale === 'vi' ? `Giao diện ${theme === 'light' ? 'sáng' : 'tối'}` : `${theme === 'light' ? 'Light' : 'Dark'} theme`}><span aria-hidden="true">{theme === 'light' ? '☼' : '☾'}</span></button><button className="lang-switch" onClick={() => setLocale(locale === 'vi' ? 'en' : 'vi')}>{locale === 'vi' ? 'EN' : 'VI'}</button><button className="nav-cta" onClick={go}>{t.primary}<span>↗</span></button></div></nav>
    <main>
      <section className="landing-hero"><div className="hero-copy"><div className="eyebrow"><i />{t.badge}</div><h1>{t.title.split('\n').map((line, i) => <span key={line}>{line}{i === 0 && <br />}</span>)}</h1><p>{t.intro}</p><div className="hero-actions"><button className="button-primary" onClick={go}>{t.primary}<span>↗</span></button><a className="button-quiet" href="#features">{t.secondary}<span>↓</span></a></div></div><div className="hero-art" aria-label="ZM AI TOOL Flow video creation workspace preview"><div className="orb orb-one" /><div className="orb orb-two" /><div className="flow-hero-shot"><img src="/previews/flow.png" alt="Flow video creation workspace" /><span>FLOW / VIDEO SERIES</span></div><div className="float-note note-top">KEYFRAME / 04 <strong>approved</strong></div><div className="float-note note-bottom"><span className="play">▶</span> Rendering your idea… <strong>84%</strong></div></div></section>
      <div className="landing-marquee"><span>{t.trusted}</span><b>✦</b><span>CREATIVE CONTROL</span><b>✦</b><span>LOCAL-FIRST WORKFLOW</span><b>✦</b><span>BUILT FOR THE BOLD</span></div>
      <section className="landing-section features-section" id="features"><div className="section-heading"><div><div className="eyebrow"><i />01 / WHY ZM</div><h2>{t.featureTitle.split('\n').map((line, i) => <span key={line}>{line}{i === 0 && <br />}</span>)}</h2></div><p>{t.featureIntro}</p></div><div className="feature-grid">{t.features.map(([num, title, text]) => <article className="feature-card" key={num}><span className="feature-num">{num}</span><div className="feature-icon">{num === '01' ? '◌' : num === '02' ? '✦' : '↗'}</div><h3>{title}</h3><p>{text}</p><a href="#workflow">Explore <span>↗</span></a></article>)}</div></section>
      <section className="landing-section product-section" id="product"><div className="section-heading"><div><div className="eyebrow"><i />02 / THE TOOLKIT</div><h2>{t.productTitle.split('\n').map((line, i) => <span key={line}>{line}{i === 0 && <br />}</span>)}</h2></div><p>{t.productIntro}</p></div><div className="product-grid">{t.productCards.map(([title, text, image, action, path]) => <article className="product-card" key={title}><div className="product-image"><img src={image} alt={title} loading="lazy" /><span className="product-chip">ZM AI TOOL</span></div><div className="product-copy"><h3>{title}</h3><p>{text}</p><button onClick={() => { window.location.href = path }}>{action}<span>↗</span></button></div></article>)}</div></section>
      <section className="landing-section workflow-section" id="workflow"><div className="workflow-visual"><div className="workflow-line" /><div className="workflow-step active"><span>01</span><div><b>IDEA</b><small>Prompt / Script / Source</small></div></div><div className="workflow-step"><span>02</span><div><b>SHAPE</b><small>Translate / Voice / Visuals</small></div></div><div className="workflow-step"><span>03</span><div><b>RELEASE</b><small>Review / Render / Share</small></div></div></div><div className="workflow-copy"><div className="eyebrow"><i />03 / THE FLOW</div><h2>{t.workflow}</h2><p>{t.workflowIntro}</p><button className="button-primary" onClick={go}>{t.primary}<span>↗</span></button></div></section>
      <section className="landing-cta"><div className="cta-glow" /><div className="eyebrow"><i />04 / START HERE</div><h2>{t.cta}</h2><p>{t.ctaText}</p><button className="button-light" onClick={go}>{t.primary}<span>↗</span></button></section>
    </main>
    <footer className="landing-footer"><a className="landing-brand" href="/landing"><span className="brand-mark">Z</span><span>ZM AI TOOL</span></a><p>{t.footer}</p><span>© 2024–2026 ZM Studio</span></footer>
  </div>
}
