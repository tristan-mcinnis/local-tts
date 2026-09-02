// Test runner for the webdemo. The tokenizer test always runs. The xfer and
// engine tests need the web model set (tools/prepare_models.sh --web) and the
// generated fixtures (npm run fixtures); when either is missing they are
// skipped with the reason printed, so `npm test` passes from a clean checkout.
import { existsSync } from "node:fs";
import { spawnSync } from "node:child_process";

const dir = new URL(".", import.meta.url).pathname;
const models = `${dir}/../models`;

const tests = [
  { file: "tokenizer.test.mjs", needs: [] },
  {
    file: "xfer.test.mjs",
    needs: [
      [`${models}/flow_lm_main_delta_flow_int8.onnx`, "web model set (tools/prepare_models.sh --web)"],
      [`${dir}/xfer_states.bin`, "generated fixtures (npm run fixtures)"],
    ],
  },
  {
    file: "engine.test.mjs",
    needs: [
      [`${models}/flow_lm_main_delta_flow_int8.onnx`, "web model set (tools/prepare_models.sh --web)"],
      [`${dir}/ref_latents.f32`, "generated fixtures (npm run fixtures)"],
      [`${dir}/ref_audio.f32`, "generated fixtures (npm run fixtures)"],
    ],
  },
];

let failed = 0, skipped = 0;
for (const t of tests) {
  const missing = t.needs.find(([path]) => !existsSync(path));
  if (missing) {
    console.log(`SKIP ${t.file}: missing ${missing[1]}`);
    skipped++;
    continue;
  }
  const r = spawnSync(process.execPath, [`${dir}/${t.file}`], { stdio: "inherit" });
  if (r.status !== 0) {
    console.log(`FAIL ${t.file} (exit ${r.status})`);
    failed++;
  } else {
    console.log(`PASS ${t.file}`);
  }
}
console.log(`${tests.length - failed - skipped} passed, ${skipped} skipped, ${failed} failed`);
process.exit(failed ? 1 : 0);
