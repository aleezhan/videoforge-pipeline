import { Config } from "@remotion/cli/config";

Config.setOverwriteOutput(true);
// Кадры — статичная иллюстрация; jpeg заметно ускоряет рендер без видимой потери.
Config.setVideoImageFormat("jpeg");
