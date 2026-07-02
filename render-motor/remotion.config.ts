import { Config } from "@remotion/cli/config";

// Rendu déterministe pour mesure reproductible (décision bloquante #1).
Config.setVideoImageFormat("jpeg");
Config.setOverwriteOutput(true);
Config.setCodec("h264");
// Chrome headless en conteneur : pas de sandbox (root), GPU off pour mesure CPU pure.
Config.setChromiumDisableWebSecurity(false);
