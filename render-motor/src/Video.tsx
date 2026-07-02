import { AbsoluteFill, Audio, Series, useCurrentFrame, useVideoConfig } from "remotion";
import { getTheme, ThemeProvider, useTheme, Defs, Backdrop, Panel, Bullet, Emphasis, Motif } from "./styleSpace/visualStyles";

// THIN scenes: the whole "look" comes from the theme (styleSpace/visualStyles.tsx).
// Content and structure are IDENTICAL across styles -> diversity = the art direction.

const SceneTitle: React.FC = () => {
  const f = useCurrentFrame();
  const { T, p, anim } = useTheme();
  const a = anim(f);
  return (
    <AbsoluteFill style={{ justifyContent: "center", alignItems: "center", textAlign: "center", fontFamily: T.font, color: p.text, filter: T.contentFilter }}>
      <div style={{ opacity: a, transform: `translateY(${(1 - a) * 30}px)` }}>
        <div style={{ fontSize: 160, fontWeight: T.bold, color: p.accent, lineHeight: 1, textTransform: T.uppercase ? "uppercase" : "none" }}>Polymnia</div>
        <div style={{ fontSize: 46, opacity: 0.85, marginTop: 18, textTransform: T.uppercase ? "uppercase" : "none" }}>
          Educational videos in <Emphasis>motion design</Emphasis>
        </div>
      </div>
    </AbsoluteFill>
  );
};

const SceneBullets: React.FC = () => {
  const f = useCurrentFrame();
  const { anim } = useTheme();
  const items = ["Per-user brand kit", "AI-first editing", "Deterministic render", "MP4 export"];
  return (
    <AbsoluteFill style={{ justifyContent: "center", alignItems: "flex-start", paddingLeft: 260 }}>
      <div>{items.map((it, i) => <Bullet key={i} a={anim(f, i * 8)} i={i}>{it}</Bullet>)}</div>
    </AbsoluteFill>
  );
};

const SceneDiagram: React.FC = () => {
  const f = useCurrentFrame();
  const { p, anim } = useTheme();
  const boxes = ["Input", "AI Pipeline", "Render"];
  return (
    <AbsoluteFill style={{ justifyContent: "center", alignItems: "center", flexDirection: "row", gap: 70 }}>
      {boxes.map((b, i) => (
        <div key={i} style={{ display: "flex", alignItems: "center", gap: 70 }}>
          <Panel a={anim(f, i * 10)}>{b}</Panel>
          {i < boxes.length - 1 && <div style={{ width: 60 * anim(f, i * 10 + 6), height: 6, background: p.accent }} />}
        </div>
      ))}
    </AbsoluteFill>
  );
};

const SceneOutro: React.FC<{ dur: number }> = ({ dur }) => {
  const f = useCurrentFrame();
  const { T, p, anim } = useTheme();
  const a = anim(f);
  const fade = Math.max(0, Math.min(1, (dur - f) / 22));
  return (
    <AbsoluteFill style={{ justifyContent: "center", alignItems: "center", opacity: fade, fontFamily: T.font }}>
      <Motif a={a} />
      <div style={{ fontSize: 72, fontWeight: T.bold, color: p.text, marginTop: 10, textTransform: T.uppercase ? "uppercase" : "none" }}><Emphasis>Subscribe</Emphasis></div>
    </AbsoluteFill>
  );
};

export const Video: React.FC<{ audioSrc: string; styleId: string; durationS?: number }> = ({ audioSrc, styleId }) => {
  const { durationInFrames } = useVideoConfig();
  const theme = getTheme(styleId);
  const per = Math.floor(durationInFrames / 4);
  return (
    <ThemeProvider theme={theme}>
      <AbsoluteFill style={{ background: theme.palette.bg }}>
        <Defs />
        <Backdrop />
        <Series>
          <Series.Sequence durationInFrames={per}><SceneTitle /></Series.Sequence>
          <Series.Sequence durationInFrames={per}><SceneBullets /></Series.Sequence>
          <Series.Sequence durationInFrames={per}><SceneDiagram /></Series.Sequence>
          <Series.Sequence durationInFrames={durationInFrames - per * 3}><SceneOutro dur={durationInFrames - per * 3} /></Series.Sequence>
        </Series>
        <Audio src={audioSrc} />
      </AbsoluteFill>
    </ThemeProvider>
  );
};
