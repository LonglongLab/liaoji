import { readFileSync } from "node:fs";

export function validateReleaseVersions(versions) {
  if (versions === null || typeof versions !== "object" || Array.isArray(versions)
      || Object.keys(versions).sort().join(",") !== "desktop,server") {
    throw new Error("Desktop and server product versions are required");
  }
  for (const key of ["desktop", "server"]) {
    const match = typeof versions[key] === "string"
      ? /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$/.exec(versions[key]) : null;
    if (match === null || (match[4] ?? "").split(".").some((part) => /^[0-9]+$/.test(part) && part.length > 1 && part.startsWith("0"))) {
      throw new Error("Invalid product release version");
    }
  }
  return versions;
}

export function releaseVersions() {
  return validateReleaseVersions(JSON.parse(readFileSync(new URL("../../backend/src/chat_archive/release_versions.json", import.meta.url), "utf8")));
}

export function versionForTarget(target, versions = releaseVersions()) {
  return target === "remote" ? versions.server : versions.desktop;
}
