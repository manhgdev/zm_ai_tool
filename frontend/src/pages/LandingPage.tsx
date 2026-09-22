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
    productTitle: 'Không chỉ là AI.\nĐây là cả một studio.', productIntro: 'Mỗi module giải quyết một đoạn việc thật trong quy trình sản xuất — từ footage thô đến video hoàn chỉnh.', productCards: [['Dịch & lồng tiếng video', 'Giữ nguyên cảm xúc, nhịp cắt và ngữ cảnh khi đưa nội dung đến khán giả mới.', '/previews/text-to-speed.png', 'Mở TTS Studio'], ['Tạo video theo series', 'Biến kịch bản thành storyboard, keyframe và chuỗi cảnh có thể duyệt trước khi render.', '/previews/flow.png', 'Khám phá Flow'], ['Làm sạch & hoàn thiện', 'Xóa watermark, làm sạch video, tạo subtitle, vẽ và xuất file trong cùng một nơi.', '/previews/clean-video.png', 'Xem bộ công cụ']],
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
    productTitle: 'Not just AI.\nA complete studio.', productIntro: 'Every module solves a real step in production — from raw footage to a finished video.', productCards: [['Translate & dub video', 'Preserve emotion, pacing and context while taking content to a new audience.', '/previews/text-to-speed.png', 'Open TTS Studio'], ['Create video series', 'Turn scripts into storyboards, keyframes and scene sequences you can approve before rendering.', '/previews/flow.png', 'Explore Flow'], ['Clean & finish', 'Remove watermarks, clean footage, create subtitles, draw and export in one place.', '/previews/clean-video.png', 'View toolkit']],
  },
} as const

export default function LandingPage() {
  const [locale, setLocale] = useState<Locale>('vi')
  useEffect(() => {
    const nodes = [document.documentElement, document.body, document.getElementById('root')].filter(Boolean) as HTMLElement[]
    const previous = nodes.map((node) => ({ node, overflow: node.style.overflow, height: node.style.height, minHeight: node.style.minHeight }))
    nodes.forEach((node) => { node.style.overflow = 'auto'; node.style.height = 'auto'; node.style.minHeight = '100%' })
    return () => previous.forEach(({ node, overflow, height, minHeight }) => { node.style.overflow = overflow; node.style.height = height; node.style.minHeight = minHeight })
  }, [])
  const t = copy[locale]
  const go = () => { window.location.href = '/text-to-speech' }
  const navTargets = ['#features', '#product', '#workflow']
  return <div className="landing"><style>{`.landing{overflow-x:clip!important;overflow-y:visible!important;padding-top:68px}.landing-nav{position:fixed!important;left:0;right:0;top:0;z-index:50;background:rgba(16,17,16,.92)!important;backdrop-filter:blur(18px)}`}</style>
    <nav className="landing-nav"><a className="landing-brand" href="/landing"><span className="brand-mark">Z</span><span>ZM AI TOOL</span></a><div className="landing-links">{t.nav.map((item, index) => <a href={navTargets[index]} key={item}>{item}</a>)}</div><div className="landing-nav-actions"><button className="lang-switch" onClick={() => setLocale(locale === 'vi' ? 'en' : 'vi')}>{locale === 'vi' ? 'EN' : 'VI'}</button><button className="nav-cta" onClick={go}>{t.primary}<span>↗</span></button></div></nav>
    <main>
      <section className="landing-hero"><div className="hero-copy"><div className="eyebrow"><i />{t.badge}</div><h1>{t.title.split('\n').map((line, i) => <span key={line}>{line}{i === 0 && <br />}</span>)}</h1><p>{t.intro}</p><div className="hero-actions"><button className="button-primary" onClick={go}>{t.primary}<span>↗</span></button><a className="button-quiet" href="#features">{t.secondary}<span>↓</span></a></div></div><div className="hero-art" aria-label="ZM AI TOOL creative workspace preview"><div className="orb orb-one" /><div className="orb orb-two" /><div className="hero-card hero-card-main"><div className="card-top"><span className="mini-logo">Z</span><span>PROJECT / 001</span><span className="live-dot">● LIVE</span></div><div className="waveform"><b /><b /><b /><b /><b /><b /><b /><b /><b /><b /><b /><b /><b /><b /><b /><b /><b /><b /></div><div className="card-caption">“Make it feel like<br /><em>the future.</em>”</div><div className="card-footer"><span>00:42:18</span><span>AI DUBBING · 98%</span></div></div><div className="float-note note-top">VOICE / VI-VN <strong>natural</strong></div><div className="float-note note-bottom"><span className="play">▶</span> Rendering your idea… <strong>84%</strong></div></div></section>
      <div className="landing-marquee"><span>{t.trusted}</span><b>✦</b><span>CREATIVE CONTROL</span><b>✦</b><span>LOCAL-FIRST WORKFLOW</span><b>✦</b><span>BUILT FOR THE BOLD</span></div>
      <section className="landing-section features-section" id="features"><div className="section-heading"><div><div className="eyebrow"><i />01 / WHY ZM</div><h2>{t.featureTitle.split('\n').map((line, i) => <span key={line}>{line}{i === 0 && <br />}</span>)}</h2></div><p>{t.featureIntro}</p></div><div className="feature-grid">{t.features.map(([num, title, text]) => <article className="feature-card" key={num}><span className="feature-num">{num}</span><div className="feature-icon">{num === '01' ? '◌' : num === '02' ? '✦' : '↗'}</div><h3>{title}</h3><p>{text}</p><a href="#workflow">Explore <span>↗</span></a></article>)}</div></section>
      <section className="landing-section product-section" id="product"><div className="section-heading"><div><div className="eyebrow"><i />02 / THE TOOLKIT</div><h2>{t.productTitle.split('\n').map((line, i) => <span key={line}>{line}{i === 0 && <br />}</span>)}</h2></div><p>{t.productIntro}</p></div><div className="product-grid">{t.productCards.map(([title, text, image, action]) => <article className="product-card" key={title}><div className="product-image"><img src={image} alt="" loading="lazy" /><span className="product-chip">ZM AI TOOL</span></div><div className="product-copy"><h3>{title}</h3><p>{text}</p><button onClick={go}>{action}<span>↗</span></button></div></article>)}</div></section>
      <section className="landing-section workflow-section" id="workflow"><div className="workflow-visual"><div className="workflow-line" /><div className="workflow-step active"><span>01</span><div><b>IDEA</b><small>Prompt / Script / Source</small></div></div><div className="workflow-step"><span>02</span><div><b>SHAPE</b><small>Translate / Voice / Visuals</small></div></div><div className="workflow-step"><span>03</span><div><b>RELEASE</b><small>Review / Render / Share</small></div></div></div><div className="workflow-copy"><div className="eyebrow"><i />03 / THE FLOW</div><h2>{t.workflow}</h2><p>{t.workflowIntro}</p><button className="button-primary" onClick={go}>{t.primary}<span>↗</span></button></div></section>
      <section className="landing-cta"><div className="cta-glow" /><div className="eyebrow"><i />04 / START HERE</div><h2>{t.cta}</h2><p>{t.ctaText}</p><button className="button-light" onClick={go}>{t.primary}<span>↗</span></button></section>
    </main>
    <footer className="landing-footer"><a className="landing-brand" href="/landing"><span className="brand-mark">Z</span><span>ZM AI TOOL</span></a><p>{t.footer}</p><span>© 2024–2026 ZM Studio</span></footer>
  </div>
}
