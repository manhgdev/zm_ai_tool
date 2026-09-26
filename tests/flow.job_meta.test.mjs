import test from "node:test";
import assert from "node:assert/strict";
import { formatFlowJobSettingsMeta } from "../frontend/src/features/flow/flow.helpers.ts";

test("video meta includes download quality", () => {
  assert.equal(
    formatFlowJobSettingsMeta("video", {
      model: "Omni 1.1 Flash",
      ratio: "16:9",
      duration: "4",
      resolution: "",
      quality: "720p",
    }),
    "Omni 1.1 Flash · 16:9 · 4s · 720p",
  );
});

test("video meta shows gen resolution and labeled download quality", () => {
  assert.equal(
    formatFlowJobSettingsMeta(
      "video",
      {
        model: "Omni 1.1 Flash",
        ratio: "16:9",
        duration: "4",
        resolution: "360p",
        quality: "720p",
      },
      { downloadLabel: "tải" },
    ),
    "Omni 1.1 Flash · 16:9 · 4s · 360p · tải 720p",
  );
});

test("image meta includes resolution", () => {
  assert.equal(
    formatFlowJobSettingsMeta("image", {
      model: "Nano Banana 2",
      ratio: "16:9",
      duration: "",
      resolution: "2K",
      quality: "",
    }),
    "Nano Banana 2 · 16:9 · 2K",
  );
});
