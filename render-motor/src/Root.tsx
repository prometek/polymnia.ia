import { Composition, staticFile } from "remotion";
import { Video } from "./Video";
import { PolymniaVideo, totalFrames, type PScene } from "./PolymniaVideo";
import { THEMES } from "./styleSpace/visualStyles";

const FPS = 60;
const WIDTH = 1920;
const HEIGHT = 1080;
const DEFAULT_DURATION_S = 60;

// Fallback scenes to open Studio without --props (replaced at real render time).
const FALLBACK_SCENES: PScene[] = [
  { layout_id: "title", durationS: 3, audio: "audio/scene-1.wav", content_data: { title: "Polymnia", subtitle: "Pipeline -> render" } },
];

// One composition per VISUAL STYLE (= art direction) -> diversity proof:
// SAME content/structure, radically different looks.
export const RemotionRoot: React.FC = () => {
  return (
    <>
      {/* DATA-DRIVEN composition: fed by the backend pipeline output. */}
      <Composition
        id="Polymnia"
        component={PolymniaVideo}
        fps={FPS}
        width={WIDTH}
        height={HEIGHT}
        defaultProps={{ styleId: "tech", scenes: FALLBACK_SCENES }}
        calculateMetadata={({ props }) => ({ durationInFrames: totalFrames(props.scenes, FPS) })}
      />

      {/* Diversity-proof compositions (fixed content). */}
      {THEMES.map((theme) => (
        <Composition
          key={theme.id}
          id={theme.id}
          component={Video}
          fps={FPS}
          width={WIDTH}
          height={HEIGHT}
          defaultProps={{ audioSrc: staticFile("audio.wav"), styleId: theme.id, durationS: DEFAULT_DURATION_S }}
          calculateMetadata={({ props }) => ({ durationInFrames: Math.round((props.durationS ?? DEFAULT_DURATION_S) * FPS) })}
        />
      ))}
    </>
  );
};
