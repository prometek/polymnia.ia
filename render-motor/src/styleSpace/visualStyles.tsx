// VISUAL STYLES — the DOMINANT axis of the style space (decision #2 reframe).
// A visual style = a full art direction (font + palette + render treatment + decor +
// animation character), NotebookLM-style (whiteboard, kawaii, ...).
// ADR-01 preserved: everything is DETERMINISTIC (SVG/CSS filters + baked assets), no genAI at render.
import React, { createContext, useContext } from "react";
import { AbsoluteFill, Img, spring, staticFile, useCurrentFrame, useVideoConfig, random } from "remotion";
import { loadFont as loadPatrick } from "@remotion/google-fonts/PatrickHand";
import { loadFont as loadBaloo } from "@remotion/google-fonts/Baloo2";
import { loadFont as loadPlayfair } from "@remotion/google-fonts/PlayfairDisplay";
import { loadFont as loadAnton } from "@remotion/google-fonts/Anton";
import { loadFont as loadMono } from "@remotion/google-fonts/SpaceMono";
import { loadFont as loadBebas } from "@remotion/google-fonts/BebasNeue";
import { loadFont as loadInter } from "@remotion/google-fonts/Inter";

const fPatrick = loadPatrick().fontFamily;
const fBaloo = loadBaloo().fontFamily;
const fPlayfair = loadPlayfair().fontFamily;
const fAnton = loadAnton().fontFamily;
const fMono = loadMono().fontFamily;
const fBebas = loadBebas().fontFamily;
const fInter = loadInter().fontFamily;

// Brand kit fonts: Google Font name -> loaded family. Used by the cosmetic override.
const FONT_MAP: Record<string, string> = {
  "Bebas Neue": fBebas,
  "Inter": fInter,
  "Space Mono": fMono,
  "Anton": fAnton,
  "Playfair Display": fPlayfair,
};
export const resolveFont = (name?: string): string | undefined =>
  name ? FONT_MAP[name] : undefined;

export type VisualStyleId = "whiteboard" | "kawaii" | "aquarelle" | "retro" | "tech";

// User-chosen background (brand kit asset). Overrides the style's procedural decor.
export type Background = {
  type: "image" | "gradient" | "solid" | "theme";
  value?: string;        // image -> file (staticFile)
  overlayDecor?: boolean; // keep the style's animated decor on top
};

export type Theme = {
  id: VisualStyleId;
  label: string;
  font: string;
  fontDisplay?: string; // title font (cosmetic override); default = font
  bold: number;
  uppercase: boolean;
  palette: { bg: string; text: string; accent: string; accent2: string; muted: string };
  spring: { damping: number; stiffness?: number };
  contentFilter?: string; // CSS filter applied to text blocks (e.g. hand-drawn stroke)
  background?: Background | null; // kit background (cosmetic override)
};

// A brand kit cosmetic: palette + type that apply ON TOP of a visual style
// (ADR-10/11). The style keeps its treatment/decor/motion; only colors and fonts
// are replaced by the kit's.
export type Cosmetic = {
  palette?: Partial<Theme["palette"]>;
  fontDisplay?: string; // Google Font name (e.g. "Bebas Neue")
  fontBody?: string;    // Google Font name (e.g. "Inter")
  uppercase?: boolean;
  background?: Background | null;
};

export const withCosmetic = (theme: Theme, cosmetic?: Cosmetic): Theme => {
  if (!cosmetic) return theme;
  return {
    ...theme,
    palette: { ...theme.palette, ...(cosmetic.palette ?? {}) },
    font: resolveFont(cosmetic.fontBody) ?? theme.font,
    fontDisplay: resolveFont(cosmetic.fontDisplay) ?? resolveFont(cosmetic.fontBody) ?? theme.font,
    uppercase: cosmetic.uppercase ?? theme.uppercase,
    background: cosmetic.background ?? theme.background ?? null,
  };
};

export const THEMES: Theme[] = [
  { id: "whiteboard", label: "Whiteboard", font: fPatrick, bold: 700, uppercase: false, palette: { bg: "#f7f6f2", text: "#1a1a1a", accent: "#1a1a1a", accent2: "#2b6cb0", muted: "#555" }, spring: { damping: 14, stiffness: 120 }, contentFilter: "url(#roughen)" },
  { id: "kawaii", label: "Kawaii", font: fBaloo, bold: 800, uppercase: false, palette: { bg: "#ffe9f2", text: "#5b3a4a", accent: "#ff6fae", accent2: "#54c9b6", muted: "#c79bb0" }, spring: { damping: 7, stiffness: 130 } },
  { id: "aquarelle", label: "Watercolor", font: fPlayfair, bold: 600, uppercase: false, palette: { bg: "#f3efe6", text: "#3a4a52", accent: "#a85c48", accent2: "#6b8aa0", muted: "#8a9aa0" }, spring: { damping: 200 } },
  { id: "retro", label: "Retro print", font: fAnton, bold: 400, uppercase: true, palette: { bg: "#f4ecd8", text: "#1f2d4a", accent: "#d6453d", accent2: "#1f2d4a", muted: "#8a8270" }, spring: { damping: 18, stiffness: 260 } },
  { id: "tech", label: "Tech / HUD", font: fMono, bold: 700, uppercase: true, palette: { bg: "#0a0e14", text: "#cfe8ff", accent: "#36e0c8", accent2: "#6cff9e", muted: "#456" }, spring: { damping: 30, stiffness: 400 } },
];

export const getTheme = (id: string): Theme => {
  const t = THEMES.find((x) => x.id === id);
  if (!t) throw new Error(`unknown visual style: ${id}`);
  return t;
};

const Ctx = createContext<Theme | null>(null);
export const ThemeProvider: React.FC<{ theme: Theme; children: React.ReactNode }> = ({ theme, children }) => <Ctx.Provider value={theme}>{children}</Ctx.Provider>;
export const useTheme = () => {
  const t = useContext(Ctx);
  if (!t) throw new Error("useTheme outside ThemeProvider");
  const { fps } = useVideoConfig();
  const anim = (localFrame: number, delay = 0) => spring({ frame: localFrame - delay, fps, config: t.spring });
  return { T: t, p: t.palette, anim };
};

// ---- Deterministic SVG filters (each style's "treatment") ----
export const Defs: React.FC = () => (
  <svg width={0} height={0} style={{ position: "absolute" }}>
    <defs>
      {/* hand-drawn stroke (whiteboard) */}
      <filter id="roughen">
        <feTurbulence type="fractalNoise" baseFrequency="0.018" numOctaves={2} result="n" />
        <feDisplacementMap in="SourceGraphic" in2="n" scale={3} />
      </filter>
      {/* irregular wash (watercolor) */}
      <filter id="watercolor">
        <feTurbulence type="fractalNoise" baseFrequency="0.012" numOctaves={3} result="n" />
        <feDisplacementMap in="SourceGraphic" in2="n" scale={22} />
        <feGaussianBlur stdDeviation={1.4} />
      </filter>
      {/* paper grain */}
      <filter id="grain">
        <feTurbulence type="fractalNoise" baseFrequency="0.9" numOctaves={2} stitchTiles="stitch" />
        <feColorMatrix type="saturate" values="0" />
      </filter>
      {/* neon glow (tech) */}
      <filter id="glow" x="-50%" y="-50%" width="200%" height="200%">
        <feGaussianBlur stdDeviation={6} result="b" />
        <feMerge><feMergeNode in="b" /><feMergeNode in="SourceGraphic" /></feMerge>
      </filter>
    </defs>
  </svg>
);

const GrainOverlay: React.FC<{ opacity: number }> = ({ opacity }) => {
  const { width, height } = useVideoConfig();
  return <svg width={width} height={height} style={{ position: "absolute", inset: 0, mixBlendMode: "multiply", opacity }}><rect width={width} height={height} filter="url(#grain)" /></svg>;
};

// Generic animated overlay: drifting particles. Keeps some life (and a realistic
// render load) when the kit background replaces the style's procedural decor.
const DriftDots: React.FC<{ color: string; n?: number }> = ({ color, n = 28 }) => {
  const frame = useCurrentFrame();
  const { width, height } = useVideoConfig();
  return (
    <svg width={width} height={height} style={{ position: "absolute", inset: 0 }}>
      {new Array(n).fill(0).map((_, i) => {
        const x = random(`bx${i}`) * width;
        const y = (random(`by${i}`) * height - frame * (0.25 + random(`bs${i}`) * 0.5) + height) % height;
        const r = 1.5 + random(`br${i}`) * 3;
        return <circle key={i} cx={x} cy={y} r={r} fill={color} opacity={0.12} />;
      })}
    </svg>
  );
};

// ---- Per-style animated backdrop (each frame differs -> realistic render load) ----
export const Backdrop: React.FC = () => {
  const { T, p } = useTheme();
  const frame = useCurrentFrame();
  const { width, height } = useVideoConfig();

  // User-chosen background (brand kit): replaces the style's procedural decor.
  const bg = T.background;
  if (bg && bg.type !== "theme") {
    let base: React.CSSProperties["background"];
    if (bg.type === "gradient") base = `linear-gradient(160deg, ${p.bg}, color-mix(in srgb, ${p.bg} 60%, black 40%))`;
    else if (bg.type === "solid") base = p.bg;
    return (
      <AbsoluteFill style={{ background: base ?? p.bg }}>
        {bg.type === "image" && bg.value && (
          <Img src={staticFile(bg.value)} style={{ width, height, objectFit: "cover" }} />
        )}
        {bg.overlayDecor && <DriftDots color={p.accent} />}
      </AbsoluteFill>
    );
  }

  if (T.id === "whiteboard") {
    const dots: React.ReactNode[] = [];
    for (let x = 40; x < width; x += 56) for (let y = 40; y < height; y += 56) dots.push(<circle key={`${x},${y}`} cx={x} cy={y} r={1.5} fill="#d8d4c8" />);
    return <AbsoluteFill style={{ background: p.bg }}><svg width={width} height={height} style={{ position: "absolute" }}>{dots}</svg></AbsoluteFill>;
  }

  if (T.id === "kawaii") {
    const motifs = new Array(14).fill(0).map((_, i) => {
      const x = random(`x${i}`) * width;
      const y = (random(`y${i}`) * height + frame * (0.4 + random(`s${i}`))) % height;
      const r = 10 + random(`r${i}`) * 22;
      const heart = i % 2 === 0;
      return heart
        ? <text key={i} x={x} y={y} fontSize={r * 2} opacity={0.5} fill={p.accent}>♥</text>
        : <text key={i} x={x} y={y} fontSize={r * 2} opacity={0.5} fill={p.accent2}>✦</text>;
    });
    return <AbsoluteFill style={{ background: `linear-gradient(160deg, #ffe1ee, #d9f6ef)` }}><svg width={width} height={height} style={{ position: "absolute" }}>{motifs}</svg></AbsoluteFill>;
  }

  if (T.id === "aquarelle") {
    const blobs = new Array(5).fill(0).map((_, i) => {
      const cx = (random(`x${i}`) * 0.8 + 0.1) * width + Math.sin(frame / 80 + i) * 30;
      const cy = (random(`y${i}`) * 0.8 + 0.1) * height + Math.cos(frame / 95 + i) * 30;
      const col = [p.accent, p.accent2, "#c7a86a", "#8aa07a"][i % 4];
      return <circle key={i} cx={cx} cy={cy} r={180 + random(`r${i}`) * 120} fill={col} opacity={0.18} filter="url(#watercolor)" />;
    });
    return <AbsoluteFill style={{ background: p.bg }}><svg width={width} height={height} style={{ position: "absolute" }}>{blobs}</svg><GrainOverlay opacity={0.06} /></AbsoluteFill>;
  }

  if (T.id === "retro") {
    const dots: React.ReactNode[] = [];
    for (let x = 0; x < width; x += 22) for (let y = 0; y < height; y += 22) dots.push(<circle key={`${x},${y}`} cx={x} cy={y} r={3} fill={p.text} opacity={0.06} />);
    const spin = (frame / 8) % 360;
    return (
      <AbsoluteFill style={{ background: p.bg }}>
        <svg width={width} height={height} style={{ position: "absolute" }}>
          <g transform={`translate(${width * 0.82} ${height * 0.2}) rotate(${spin})`}>
            {new Array(12).fill(0).map((_, i) => <rect key={i} x={-6} y={-160} width={12} height={70} fill={p.accent} opacity={0.5} transform={`rotate(${i * 30})`} />)}
          </g>
          {dots}
        </svg>
        <GrainOverlay opacity={0.08} />
      </AbsoluteFill>
    );
  }

  // tech
  const off = (frame % 60) / 60;
  const lines: React.ReactNode[] = [];
  for (let i = -1; i < 34; i++) lines.push(<line key={`v${i}`} x1={(i + off) * 64} y1={0} x2={(i + off) * 64} y2={height} stroke={p.accent} strokeWidth={1} opacity={0.08} />);
  for (let i = -1; i < 20; i++) lines.push(<line key={`h${i}`} x1={0} y1={(i + off) * 64} x2={width} y2={(i + off) * 64} stroke={p.accent} strokeWidth={1} opacity={0.08} />);
  const scan: React.ReactNode[] = [];
  for (let y = 0; y < height; y += 4) scan.push(<line key={`s${y}`} x1={0} y1={y} x2={width} y2={y} stroke="#000" strokeWidth={1} opacity={0.18} />);
  // Depth derived from the theme/kit background (p.bg): lighter center, darker edges.
  const techBg = `radial-gradient(circle at 50% 40%, color-mix(in srgb, ${p.bg} 90%, white 8%), color-mix(in srgb, ${p.bg} 75%, black 25%))`;
  return <AbsoluteFill style={{ background: techBg }}><svg width={width} height={height} style={{ position: "absolute" }}>{lines}{scan}</svg></AbsoluteFill>;
};

// ---- Panel = themed diagram box ----
export const Panel: React.FC<{ children: React.ReactNode; a: number }> = ({ children, a }) => {
  const { T, p } = useTheme();
  const common: React.CSSProperties = { padding: "26px 34px", fontFamily: T.font, fontWeight: T.bold, fontSize: 40, color: p.text, opacity: a, textTransform: T.uppercase ? "uppercase" : "none", whiteSpace: "nowrap" };
  switch (T.id) {
    case "whiteboard":
      return <div style={{ ...common, background: "#fff", border: `3px solid ${p.text}`, filter: "url(#roughen)", transform: `rotate(-1deg) scale(${0.9 + a * 0.1})` }}>{children}</div>;
    case "kawaii":
      return <div style={{ ...common, background: "#fff", border: `5px solid ${p.accent2}`, borderRadius: 30, boxShadow: "0 10px 0 rgba(255,111,174,0.25)", transform: `scale(${0.7 + a * 0.3})` }}>{children}</div>;
    case "aquarelle":
      return <div style={{ ...common, background: "rgba(107,138,160,0.18)", borderRadius: 8, boxShadow: "inset 0 0 0 2px rgba(107,138,160,0.4)", transform: `scale(${0.92 + a * 0.08})` }}>{children}</div>;
    case "retro":
      return <div style={{ ...common, background: p.accent2, color: p.bg, boxShadow: `8px 8px 0 ${p.accent}`, transform: `translate(${(1 - a) * 12}px, ${(1 - a) * 12}px)` }}>{children}</div>;
    case "tech":
      return <div style={{ ...common, background: "rgba(54,224,200,0.06)", border: `1px solid ${p.accent}`, color: p.text, filter: "url(#glow)", transform: `scale(${0.95 + a * 0.05})` }}>{children}</div>;
  }
};

// ---- Bullet = themed bullet row ----
// If a brand-kit `icon` (emoji) is given, it REPLACES the themed mark (no double bullet).
export const Bullet: React.FC<{ children: React.ReactNode; a: number; i: number; icon?: string }> = ({ children, a, i, icon }) => {
  const { T, p } = useTheme();
  const row: React.CSSProperties = { display: "flex", alignItems: "center", gap: 22, margin: "20px 0", fontFamily: T.font, fontWeight: T.bold, fontSize: 50, color: p.text, opacity: a, textTransform: T.uppercase ? "uppercase" : "none", transform: `translateX(${(1 - a) * -40}px)` };
  let mark: React.ReactNode;
  if (icon) {
    mark = <span style={{ fontSize: 44 }}>{icon}</span>;
  } else {
    switch (T.id) {
      case "whiteboard": mark = <span style={{ filter: "url(#roughen)", border: `3px solid ${p.text}`, width: 30, height: 30, display: "inline-block" }} />; break;
      case "kawaii": mark = <span style={{ color: p.accent, fontSize: 44 }}>♥</span>; break;
      case "aquarelle": mark = <span style={{ width: 26, height: 26, borderRadius: "50%", background: p.accent, opacity: 0.7, display: "inline-block", filter: "url(#watercolor)" }} />; break;
      case "retro": mark = <span style={{ width: 28, height: 28, background: p.accent, display: "inline-block" }} />; break;
      case "tech": mark = <span style={{ color: p.accent, filter: "url(#glow)" }}>{">"}</span>; break;
    }
  }
  return <div style={row}>{mark}<span>{children}</span></div>;
};

// ---- Emphasis = highlighted keyword ----
export const Emphasis: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { T, p } = useTheme();
  switch (T.id) {
    case "whiteboard": return <span style={{ borderBottom: `4px solid ${p.accent2}`, filter: "url(#roughen)", color: p.text }}>{children}</span>;
    case "kawaii": return <span style={{ background: p.accent, color: "#fff", borderRadius: 16, padding: "0 14px" }}>{children}</span>;
    case "aquarelle": return <span style={{ background: "rgba(168,92,72,0.22)", color: p.accent, padding: "0 8px", filter: "url(#watercolor)" }}>{children}</span>;
    case "retro": return <span style={{ background: p.accent, color: p.bg, padding: "0 10px" }}>{children}</span>;
    case "tech": return <span style={{ color: p.accent, filter: "url(#glow)" }}>[{children}]</span>;
  }
};

// ---- Outro motif representative of the style ----
export const Motif: React.FC<{ a: number }> = ({ a }) => {
  const { T, p } = useTheme();
  const rot = a * 180;
  switch (T.id) {
    case "whiteboard":
      return <svg width={300} height={300} viewBox="0 0 300 300" style={{ filter: "url(#roughen)" }}><polygon points="150,30 188,115 280,120 208,180 232,270 150,220 68,270 92,180 20,120 112,115" fill="none" stroke={p.text} strokeWidth={5} /></svg>;
    case "kawaii":
      return <svg width={300} height={300} viewBox="0 0 300 300"><ellipse cx="150" cy="160" rx={110 * a} ry={100 * a} fill="#fff" stroke={p.accent2} strokeWidth={6} /><circle cx="120" cy="150" r="12" fill={p.text} /><circle cx="180" cy="150" r="12" fill={p.text} /><circle cx="100" cy="180" r="14" fill={p.accent} opacity={0.6} /><circle cx="200" cy="180" r="14" fill={p.accent} opacity={0.6} /><path d="M130 190 Q150 210 170 190" stroke={p.text} strokeWidth={5} fill="none" /></svg>;
    case "aquarelle":
      return <svg width={300} height={300} viewBox="0 0 300 300"><circle cx="150" cy="150" r={110 * a} fill={p.accent} opacity={0.4} filter="url(#watercolor)" /><circle cx="170" cy="140" r={70 * a} fill={p.accent2} opacity={0.4} filter="url(#watercolor)" /></svg>;
    case "retro":
      return <svg width={300} height={300} viewBox="0 0 300 300"><g transform={`rotate(${rot} 150 150)`}>{new Array(10).fill(0).map((_, i) => <rect key={i} x={140} y={20} width={20} height={70} fill={i % 2 ? p.accent : p.accent2} transform={`rotate(${i * 36} 150 150)`} />)}</g></svg>;
    case "tech":
      return <svg width={300} height={300} viewBox="0 0 300 300" style={{ filter: "url(#glow)" }}><circle cx="150" cy="150" r={110 * a} fill="none" stroke={p.accent} strokeWidth={3} strokeDasharray="20 12" transform={`rotate(${rot} 150 150)`} /><circle cx="150" cy="150" r={70 * a} fill="none" stroke={p.accent2} strokeWidth={2} /></svg>;
  }
};
