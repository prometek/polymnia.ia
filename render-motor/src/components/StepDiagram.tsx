// StepDiagram — tool-call native component.
// Renders the `props` produced by the StepDiagram tool call. Each step has a
// `startFrame` (computed backend-side from its narration_cue) so it pops in sync
// with the voiceover. Colors come from the active theme (cosmetic override).
import { useCurrentFrame, interpolate, Easing } from "remotion";
import { useTheme } from "../styleSpace/visualStyles";

export interface Step {
  label: string;
  description: string;
  tag?: string;
  startFrame: number; // frame when this step appears (relative to scene start)
}

export interface StepDiagramProps {
  steps: Step[];
}

export const StepDiagram: React.FC<{ props: StepDiagramProps }> = ({ props }) => {
  const frame = useCurrentFrame();
  const { T, p } = useTheme();
  const steps = props.steps ?? [];

  return (
    <div style={{
      width: "100%", height: "100%",
      padding: "120px 180px",
      fontFamily: T.font,
      display: "flex", flexDirection: "column", justifyContent: "center",
    }}>
      {steps.map((step, i) => {
        const entered = frame >= step.startFrame;
        const progress = entered
          ? interpolate(frame, [step.startFrame, step.startFrame + 18], [0, 1], {
              easing: Easing.out(Easing.cubic), extrapolateRight: "clamp",
            })
          : 0;
        const isActive =
          i === steps.length - 1
            ? entered
            : entered && frame < (steps[i + 1]?.startFrame ?? Infinity);

        return (
          <div key={i} style={{ display: "flex", gap: 32, opacity: progress }}>
            {/* Left column: number + connector */}
            <div style={{ display: "flex", flexDirection: "column", alignItems: "center" }}>
              <div style={{
                width: 64, height: 64, borderRadius: "50%",
                border: `3px solid ${isActive ? p.accent : p.muted}`,
                background: isActive ? p.accent : "transparent",
                color: isActive ? p.bg : p.text,
                display: "flex", alignItems: "center", justifyContent: "center",
                fontFamily: T.fontDisplay ?? T.font, fontSize: 30, fontWeight: T.bold,
                flexShrink: 0,
              }}>
                {i + 1}
              </div>
              {i < steps.length - 1 && (
                <div style={{ width: 3, flex: 1, minHeight: 48, background: p.muted, opacity: 0.4, marginTop: 8 }} />
              )}
            </div>

            {/* Content */}
            <div style={{ paddingBottom: 44, transform: `translateX(${interpolate(progress, [0, 1], [-24, 0])}px)` }}>
              {step.tag && (
                <div style={{
                  display: "inline-block", fontSize: 18, padding: "4px 14px", borderRadius: 6,
                  background: p.accent + "22", color: p.accent, marginBottom: 10, fontWeight: 600,
                  textTransform: T.uppercase ? "uppercase" : "none",
                }}>
                  {step.tag}
                </div>
              )}
              <div style={{ fontSize: 40, fontWeight: T.bold, color: p.text, marginBottom: 8, lineHeight: 1.25, fontFamily: T.fontDisplay ?? T.font, textTransform: T.uppercase ? "uppercase" : "none" }}>
                {step.label}
              </div>
              <div style={{ fontSize: 26, color: p.muted, lineHeight: 1.5, maxWidth: 900 }}>
                {step.description}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
};
