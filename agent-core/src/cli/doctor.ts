import { createCoreNative } from "../daemon/daemon.js";
import { ProviderDetector } from "../doctor/provider-detector.js";
import { redact } from "../logs/redact.js";
import { CoreNative } from "../native/core.js";

export interface DoctorDeps {
  detector?: ProviderDetector;
  core?: CoreNative;
  log?: (line: string) => void;
}

export async function runDoctor(deps: DoctorDeps = {}): Promise<void> {
  const detector = deps.detector ?? new ProviderDetector();
  const core = deps.core ?? createCoreNative();
  const log = deps.log ?? console.log;
  try {
    log(
      JSON.stringify(
        {
          core: await coreStatus(core),
          environment: await detector.detect(),
        },
        null,
        2,
      ),
    );
  } finally {
    await core.shutdown?.();
  }
}

async function coreStatus(core: CoreNative) {
  try {
    return await core.openOrCreateDevice({});
  } catch (error) {
    return {
      connected: false,
      status: "unavailable",
      reason: redact(error instanceof Error ? error.message : String(error)),
    };
  }
}
