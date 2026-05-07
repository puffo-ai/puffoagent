import fs from "node:fs/promises";
import path from "node:path";

const binPath = path.resolve("dist", "src", "cli", "index.js");

try {
  await fs.chmod(binPath, 0o755);
} catch (error) {
  if (!error || error.code !== "ENOENT") throw error;
}
