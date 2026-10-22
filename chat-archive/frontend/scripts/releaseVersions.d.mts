export function releaseVersions(): { desktop: string; server: string };
export function validateReleaseVersions(value: unknown): { desktop: string; server: string };
export function versionForTarget(target: string, versions?: { desktop: string; server: string }): string;
