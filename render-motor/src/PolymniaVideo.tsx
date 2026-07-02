// DATA-DRIVEN composition: renders a video from the backend pipeline output
// (scene_audio.json -> render-input.json). Each scene = layout + content_data + audio + duration.
// The "look" comes from the theme (styleSpace/visualStyles.tsx); the content comes from the props.
import { AbsoluteFill, Audio, Img, Sequence, interpolate, staticFile, useCurrentFrame, useVideoConfig } from "remotion";
import { TransitionSeries, linearTiming } from "@remotion/transitions";
import { fade } from "@remotion/transitions/fade";
import { slide } from "@remotion/transitions/slide";
import {
  getTheme, withCosmetic, ThemeProvider, useTheme, Defs, Backdrop, Panel, Bullet, Emphasis, Motif,
  type Cosmetic,
} from "./styleSpace/visualStyles";
import { StepDiagram } from "./components/StepDiagram";

// ---- Props types (mirror of render-input.json) ----
export type PItem = { text: string; icon?: string; startFrame?: number; narration_cue?: string };
export type PNode = { id: string; label: string; startFrame?: number; narration_cue?: string };
export type PColumn = { title: string; items: string[] };
export type PStep = { label: string; detail?: string };
export type PStat = { value: string; label: string; startFrame?: number; narration_cue?: string };
export type PSeries = { label: string; value: number };
export type PLayoutId =
  | "title" | "bullets" | "diagram" | "definition"
  | "comparison" | "steps" | "stat" | "chart"
  | "section" | "statement" | "image" | "outro";
export type Composition = "centered" | "left" | "right" | "full";
export type PContent = {
  title?: string; subtitle?: string;
  items?: PItem[];
  nodes?: PNode[];
  cta?: string;
  term?: string; definition?: string;
  left?: PColumn; right?: PColumn;
  steps?: PStep[];
  stats?: PStat[];
  chart_type?: "bar" | "line" | "pie"; series?: PSeries[]; caption?: string;
  kicker?: string;          // section
  text?: string; emphasis?: string; // statement
  glyph?: string;           // image
};
export type PScene = {
  // Tool-call shape: type = component name, props = tool args. When present, takes
  // precedence over (layout_id, content_data). Used by the StepDiagram demo.
  type?: string;
  props?: Record<string, unknown>;
  layout_id?: PLayoutId;
  composition?: Composition;
  durationS: number;
  audio: string; // path relative to public/ (staticFile)
  content_data?: PContent;
};
export type PolymniaProps = { styleId: string; scenes: PScene[]; cosmetic?: Cosmetic; logo?: string | null };

const framesOf = (durationS: number, fps: number) => Math.max(1, Math.round(durationS * fps));

// Reveal progress (0..1): narration-synced from startFrame if present, else fallback.
const cueReveal = (frame: number, startFrame: number | undefined, fallback: number) =>
  startFrame != null
    ? interpolate(frame, [startFrame, startFrame + 12], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" })
    : fallback;

// ---- Pacing: breathing room around the voiceover ----
// The scene lasts a bit longer than the audio: lead-in (content set up before the voice) +
// tail (lets the voice finish / the transition lands on silence, not on speech).
const LEAD = 14;   // frames before the voice starts
const TAIL = 22;   // frames after the voice ends
const TRANS = 16;  // transition duration between scenes (overlap)

const sceneFrames = (durationS: number, fps: number) => framesOf(durationS, fps) + LEAD + TAIL;

// Per-scene envelope: continuous micro-motion (life) + exit fade (exit animation).
const SceneWrap: React.FC<{ children: React.ReactNode; dur: number }> = ({ children, dur }) => {
  const f = useCurrentFrame();
  const drift = Math.sin(f / 42) * 4;                     // slight vertical bob
  const scale = 1 + Math.sin(f / 90) * 0.004;             // very subtle breathing
  const out = Math.max(0, Math.min(1, (dur - f) / 14));   // exit fade (last 14 frames)
  return (
    <AbsoluteFill style={{ opacity: out, transform: `translateY(${drift}px) scale(${scale})` }}>
      {children}
    </AbsoluteFill>
  );
};

// Brand kit logo (baked asset). Shown on the title and the outro.
const Logo: React.FC<{ src: string; size: number; a?: number }> = ({ src, size, a = 1 }) => (
  <Img src={staticFile(src)} style={{ width: size, height: size, opacity: a }} />
);

// Safe-zone margin (broadcast) applied to each scene.
const SAFE = 110;
// Title size adapted to length (avoids overflow on long titles).
const fitTitle = (t?: string) => {
  const n = (t ?? "").length;
  return n <= 14 ? 150 : n <= 24 ? 122 : n <= 36 ? 98 : 80;
};

// ---- Composition (placement): positions the content block in the frame ----
const placeStyle = (c: Composition = "centered"): React.CSSProperties => {
  switch (c) {
    case "left":  return { justifyContent: "center", alignItems: "flex-start", textAlign: "left", paddingLeft: 200, paddingRight: 140 };
    case "right": return { justifyContent: "center", alignItems: "flex-end", textAlign: "right", paddingLeft: 140, paddingRight: 200 };
    case "full":  return { justifyContent: "center", alignItems: "center", textAlign: "center", padding: 60 };
    default:      return { justifyContent: "center", alignItems: "center", textAlign: "center", padding: SAFE };
  }
};

const Place: React.FC<{ c?: Composition; children: React.ReactNode; style?: React.CSSProperties }> = ({ c, children, style }) => (
  <AbsoluteFill style={{ display: "flex", flexDirection: "column", ...placeStyle(c), ...style }}>
    {children}
  </AbsoluteFill>
);

// ---- One layout = one component. Content arrives via props. ----
const Title: React.FC<{ cd: PContent; logo?: string | null; c?: Composition }> = ({ cd, logo, c }) => {
  const f = useCurrentFrame();
  const { T, p, anim } = useTheme();
  const a = anim(f);
  const big = c === "full" ? 1.18 : 1;
  return (
    <Place c={c} style={{ fontFamily: T.font, color: p.text, filter: T.contentFilter }}>
      <div style={{ opacity: a, transform: `translateY(${(1 - a) * 30}px)`, maxWidth: c === "full" ? 1750 : 1600 }}>
        {logo && c !== "right" && <div style={{ display: "flex", justifyContent: c === "left" ? "flex-start" : "center", marginBottom: 28 }}><Logo src={logo} size={140} a={a} /></div>}
        <div style={{ fontFamily: T.fontDisplay ?? T.font, fontSize: fitTitle(cd.title) * big, fontWeight: T.bold, color: p.accent, lineHeight: 1.05, textTransform: T.uppercase ? "uppercase" : "none" }}>{cd.title}</div>
        {cd.subtitle && (
          <div style={{ fontSize: 44, opacity: 0.85, marginTop: 20, lineHeight: 1.3, textTransform: T.uppercase ? "uppercase" : "none" }}>{cd.subtitle}</div>
        )}
      </div>
    </Place>
  );
};

const Bullets: React.FC<{ cd: PContent; c?: Composition }> = ({ cd, c }) => {
  const f = useCurrentFrame();
  const { anim } = useTheme();
  const items = cd.items ?? [];
  const right = c === "right";
  // Narration-synced reveal when startFrame is present, else a fixed stagger.
  const reveal = (it: PItem, i: number) =>
    it.startFrame != null
      ? interpolate(f, [it.startFrame, it.startFrame + 12], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" })
      : anim(f, i * 8);
  return (
    <AbsoluteFill style={{ justifyContent: "center", alignItems: right ? "flex-end" : "flex-start", paddingLeft: right ? 160 : 260, paddingRight: right ? 260 : 160 }}>
      <div style={{ textAlign: right ? "right" : "left" }}>
        {items.map((it, i) => (
          <Bullet key={i} a={reveal(it, i)} i={i} icon={it.icon}>
            {it.text}
          </Bullet>
        ))}
      </div>
    </AbsoluteFill>
  );
};

const Diagram: React.FC<{ cd: PContent }> = ({ cd }) => {
  const f = useCurrentFrame();
  const { p, anim } = useTheme();
  const nodes = cd.nodes ?? [];
  return (
    <AbsoluteFill style={{ justifyContent: "center", alignItems: "center", flexDirection: "row", gap: 60, flexWrap: "wrap", padding: 120 }}>
      {nodes.map((n, i) => {
        const a = cueReveal(f, n.startFrame, anim(f, i * 10));
        return (
          <div key={n.id ?? i} style={{ display: "flex", alignItems: "center", gap: 60 }}>
            <Panel a={a}>{n.label}</Panel>
            {i < nodes.length - 1 && <div style={{ width: 56 * a, height: 6, background: p.accent }} />}
          </div>
        );
      })}
    </AbsoluteFill>
  );
};

const Definition: React.FC<{ cd: PContent; c?: Composition }> = ({ cd, c }) => {
  const f = useCurrentFrame();
  const { T, p, anim } = useTheme();
  const a = anim(f);
  return (
    <Place c={c} style={{ fontFamily: T.font }}>
      <div style={{ opacity: a, transform: `translateY(${(1 - a) * 24}px)`, maxWidth: 1400 }}>
        <div style={{ fontFamily: T.fontDisplay ?? T.font, fontSize: fitTitle(cd.term), fontWeight: T.bold, color: p.accent, lineHeight: 1.02, textTransform: T.uppercase ? "uppercase" : "none" }}>{cd.term}</div>
        <div style={{ fontSize: 46, color: p.text, marginTop: 28, lineHeight: 1.4 }}>{cd.definition}</div>
      </div>
    </Place>
  );
};

const Comparison: React.FC<{ cd: PContent }> = ({ cd }) => {
  const f = useCurrentFrame();
  const { T, p, anim } = useTheme();
  const col = (c: PColumn | undefined, side: number) => {
    const a = anim(f, side * 8);
    return (
      <div style={{ flex: 1, opacity: a, transform: `translateX(${(1 - a) * (side ? 40 : -40)}px)` }}>
        <div style={{ fontFamily: T.fontDisplay ?? T.font, fontSize: 56, fontWeight: T.bold, color: p.accent, marginBottom: 28, textTransform: T.uppercase ? "uppercase" : "none" }}>{c?.title}</div>
        {(c?.items ?? []).map((it, i) => (
          <div key={i} style={{ display: "flex", gap: 16, margin: "16px 0", fontSize: 38, color: p.text }}>
            <span style={{ color: p.accent }}>•</span><span>{it}</span>
          </div>
        ))}
      </div>
    );
  };
  return (
    <AbsoluteFill style={{ justifyContent: "center", alignItems: "stretch", flexDirection: "row", gap: 80, padding: "180px 160px", fontFamily: T.font }}>
      {col(cd.left, 0)}
      <div style={{ width: 4, background: p.muted, opacity: 0.5 }} />
      {col(cd.right, 1)}
    </AbsoluteFill>
  );
};

// (steps -> StepDiagram component, narration-synced; see components/StepDiagram.tsx)

const Stat: React.FC<{ cd: PContent; c?: Composition }> = ({ cd, c }) => {
  const f = useCurrentFrame();
  const { T, p, anim } = useTheme();
  const stats = cd.stats ?? [];
  const big = c === "full" ? 1.25 : 1;
  const base = stats.length > 1 ? 180 : 240;
  return (
    <AbsoluteFill style={{ justifyContent: "center", alignItems: "center", flexDirection: "row", gap: 120, fontFamily: T.font, padding: 120, flexWrap: "wrap" }}>
      {stats.map((s, i) => {
        const a = cueReveal(f, s.startFrame, anim(f, i * 10));
        return (
          <div key={i} style={{ textAlign: "center", opacity: a, transform: `scale(${0.7 + a * 0.3})` }}>
            <div style={{ fontFamily: T.fontDisplay ?? T.font, fontSize: base * big, fontWeight: T.bold, color: p.accent, lineHeight: 1 }}>{s.value}</div>
            <div style={{ fontSize: 42, color: p.text, marginTop: 16, textTransform: T.uppercase ? "uppercase" : "none" }}>{s.label}</div>
          </div>
        );
      })}
    </AbsoluteFill>
  );
};

// ---- SVG chart (bar | line | pie), deterministic and themed ----
const Chart: React.FC<{ cd: PContent }> = ({ cd }) => {
  const f = useCurrentFrame();
  const { T, p, anim } = useTheme();
  const a = anim(f);
  const series = cd.series ?? [];
  const W = 1200, H = 560, PAD = 70;
  const colors = [p.accent, p.accent2, p.muted, p.text];
  const max = Math.max(1, ...series.map((s) => s.value));
  const innerW = W - PAD * 2, innerH = H - PAD * 2;

  const body = () => {
    if (cd.chart_type === "pie") {
      const total = series.reduce((n, s) => n + s.value, 0) || 1;
      const cx = W / 2, cy = H / 2, r = Math.min(innerW, innerH) / 2;
      let start = -Math.PI / 2;
      return series.map((s, i) => {
        const frac = (s.value / total) * a;
        const end = start + frac * 2 * Math.PI;
        const x1 = cx + r * Math.cos(start), y1 = cy + r * Math.sin(start);
        const x2 = cx + r * Math.cos(end), y2 = cy + r * Math.sin(end);
        const large = end - start > Math.PI ? 1 : 0;
        const d = `M${cx},${cy} L${x1},${y1} A${r},${r} 0 ${large} 1 ${x2},${y2} Z`;
        start = end;
        return <path key={i} d={d} fill={colors[i % colors.length]} opacity={0.9} />;
      });
    }
    if (cd.chart_type === "line") {
      const stepX = series.length > 1 ? innerW / (series.length - 1) : 0;
      const pts = series.map((s, i) => [PAD + i * stepX, PAD + innerH - (s.value / max) * innerH * a]);
      const dPath = pts.map((pt, i) => `${i ? "L" : "M"}${pt[0]},${pt[1]}`).join(" ");
      return (
        <>
          <path d={dPath} fill="none" stroke={p.accent} strokeWidth={5} />
          {pts.map((pt, i) => <circle key={i} cx={pt[0]} cy={pt[1]} r={8} fill={p.accent} />)}
          {series.map((s, i) => <text key={i} x={PAD + i * stepX} y={H - 24} fill={p.text} fontSize={26} textAnchor="middle" fontFamily={T.font}>{s.label}</text>)}
        </>
      );
    }
    // bar
    const bw = innerW / series.length * 0.6;
    const gap = innerW / series.length;
    return series.map((s, i) => {
      const h = (s.value / max) * innerH * a;
      const x = PAD + i * gap + (gap - bw) / 2;
      const y = PAD + innerH - h;
      return (
        <g key={i}>
          <rect x={x} y={y} width={bw} height={h} fill={colors[i % colors.length]} />
          <text x={x + bw / 2} y={y - 12} fill={p.text} fontSize={28} textAnchor="middle" fontFamily={T.font}>{s.value}</text>
          <text x={x + bw / 2} y={H - 24} fill={p.text} fontSize={26} textAnchor="middle" fontFamily={T.font}>{s.label}</text>
        </g>
      );
    });
  };

  return (
    <AbsoluteFill style={{ justifyContent: "center", alignItems: "center", fontFamily: T.font }}>
      <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`}>
        {cd.chart_type !== "pie" && <line x1={PAD} y1={H - PAD} x2={W - PAD} y2={H - PAD} stroke={p.muted} strokeWidth={2} />}
        {body()}
      </svg>
      {cd.caption && <div style={{ fontSize: 34, color: p.muted, marginTop: 10 }}>{cd.caption}</div>}
    </AbsoluteFill>
  );
};

const Section: React.FC<{ cd: PContent; c?: Composition }> = ({ cd, c }) => {
  const f = useCurrentFrame();
  const { T, p, anim } = useTheme();
  const a = anim(f);
  return (
    <Place c={c} style={{ fontFamily: T.font }}>
      <div style={{ opacity: a, transform: `translateY(${(1 - a) * 26}px)`, maxWidth: 1500 }}>
        {cd.kicker && (
          <div style={{ fontFamily: T.font, fontSize: 34, letterSpacing: ".18em", color: p.accent, textTransform: "uppercase", marginBottom: 18 }}>{cd.kicker}</div>
        )}
        <div style={{ width: 90, height: 6, background: p.accent, margin: c === "left" ? "0 0 26px 0" : "0 auto 26px", opacity: a }} />
        <div style={{ fontFamily: T.fontDisplay ?? T.font, fontSize: fitTitle(cd.title) * (c === "full" ? 1.15 : 1), fontWeight: T.bold, color: p.text, lineHeight: 1.04, textTransform: T.uppercase ? "uppercase" : "none" }}>{cd.title}</div>
      </div>
    </Place>
  );
};

const Statement: React.FC<{ cd: PContent; c?: Composition }> = ({ cd, c }) => {
  const f = useCurrentFrame();
  const { T, p, anim } = useTheme();
  const a = anim(f);
  const text = cd.text ?? "";
  const emph = cd.emphasis;
  // Split the sentence around the group to emphasize.
  let body: React.ReactNode = text;
  if (emph && text.includes(emph)) {
    const [before, after] = text.split(emph);
    body = <>{before}<Emphasis>{emph}</Emphasis>{after}</>;
  }
  const size = text.length <= 40 ? 96 : text.length <= 80 ? 76 : 60;
  return (
    <Place c={c} style={{ fontFamily: T.font, color: p.text }}>
      <div style={{ opacity: a, transform: `scale(${0.96 + a * 0.04})`, maxWidth: 1700, fontFamily: T.fontDisplay ?? T.font, fontWeight: T.bold, fontSize: size * (c === "full" ? 1.15 : 1), lineHeight: 1.12, textTransform: T.uppercase ? "uppercase" : "none" }}>
        {body}
      </div>
    </Place>
  );
};

const ImageScene: React.FC<{ cd: PContent; c?: Composition }> = ({ cd, c }) => {
  const f = useCurrentFrame();
  const { T, p, anim } = useTheme();
  const a = anim(f);
  return (
    <Place c={c} style={{ fontFamily: T.font, color: p.text }}>
      <div style={{ opacity: a, transform: `scale(${0.7 + a * 0.3})`, fontSize: 320, lineHeight: 1 }}>{cd.glyph || "✦"}</div>
      {cd.caption && (
        <div style={{ opacity: a, fontSize: 44, color: p.text, marginTop: 24, maxWidth: 1200, lineHeight: 1.3 }}>{cd.caption}</div>
      )}
    </Place>
  );
};

const Outro: React.FC<{ cd: PContent; dur: number; logo?: string | null }> = ({ cd, dur, logo }) => {
  const f = useCurrentFrame();
  const { T, p, anim } = useTheme();
  const a = anim(f);
  const fade = Math.max(0, Math.min(1, (dur - f) / 22));
  return (
    <AbsoluteFill style={{ justifyContent: "center", alignItems: "center", opacity: fade, fontFamily: T.font, textAlign: "center", padding: 120 }}>
      {logo ? <Logo src={logo} size={180} a={a} /> : <Motif a={a} />}
      <div style={{ fontSize: 64, fontWeight: T.bold, color: p.text, marginTop: 20, textTransform: T.uppercase ? "uppercase" : "none" }}>
        <Emphasis>{cd.cta}</Emphasis>
      </div>
    </AbsoluteFill>
  );
};

const SceneByLayout: React.FC<{ scene: PScene; durFrames: number; logo?: string | null }> = ({ scene, durFrames, logo }) => {
  // Tool-call shape: type = component, props = content. (content_data = legacy fallback.)
  const cd = (scene.props ?? scene.content_data ?? {}) as PContent;
  const c = scene.composition;
  const type = scene.type ?? scene.layout_id;
  switch (type) {
    case "title": return <Title cd={cd} logo={logo} c={c} />;
    case "bullets": return <Bullets cd={cd} c={c} />;
    case "diagram": return <Diagram cd={cd} />;
    case "definition": return <Definition cd={cd} c={c} />;
    case "comparison": return <Comparison cd={cd} />;
    case "steps": return <StepDiagram props={scene.props as any} />;
    case "stat": return <Stat cd={cd} c={c} />;
    case "chart": return <Chart cd={cd} />;
    case "section": return <Section cd={cd} c={c} />;
    case "statement": return <Statement cd={cd} c={c} />;
    case "image": return <ImageScene cd={cd} c={c} />;
    case "outro": return <Outro cd={cd} dur={durFrames} logo={logo} />;
    default: return <Title cd={cd} logo={logo} c={c} />;
  }
};

// Alternating transitions (a little variety without breaking consistency).
const presentationFor = (i: number) =>
  i % 2 === 0 ? fade() : slide({ direction: "from-right" });

export const PolymniaVideo: React.FC<PolymniaProps> = ({ styleId, scenes, cosmetic, logo }) => {
  const { fps } = useVideoConfig();
  const theme = withCosmetic(getTheme(styleId), cosmetic);
  return (
    <ThemeProvider theme={theme}>
      <AbsoluteFill style={{ background: theme.palette.bg }}>
        <Defs />
        <Backdrop />
        <TransitionSeries>
          {scenes.flatMap((scene, i) => {
            const dur = sceneFrames(scene.durationS, fps);
            const seq = (
              <TransitionSeries.Sequence key={`s${i}`} durationInFrames={dur}>
                <SceneWrap dur={dur}>
                  <SceneByLayout scene={scene} durFrames={dur} logo={logo} />
                </SceneWrap>
                {/* Voiceover offset by the lead-in -> content settles before it starts. */}
                <Sequence from={LEAD}>
                  <Audio src={staticFile(scene.audio)} />
                </Sequence>
              </TransitionSeries.Sequence>
            );
            if (i === scenes.length - 1) return [seq];
            return [
              seq,
              <TransitionSeries.Transition
                key={`t${i}`}
                timing={linearTiming({ durationInFrames: TRANS })}
                presentation={presentationFor(i)}
              />,
            ];
          })}
        </TransitionSeries>
      </AbsoluteFill>
    </ThemeProvider>
  );
};

// Total duration (driven by the voiceover, ADR-08): sum of scenes (audio + pacing)
// minus the transition overlaps.
export const totalFrames = (scenes: PScene[], fps: number) => {
  const total = scenes.reduce((n, s) => n + sceneFrames(s.durationS, fps), 0)
    - Math.max(0, scenes.length - 1) * TRANS;
  return total || fps;
};
