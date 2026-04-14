/** Convert a raw output path to a /media/ URL for the browser. */
export function toMediaUrl(rawPath: string): string {
  // Handle relative paths like "output/final/video.mp4"
  if (rawPath.startsWith("output/")) {
    return "/media/" + rawPath.slice("output/".length);
  }
  // Handle absolute paths like "/Users/.../output/final/video.mp4"
  const marker = "/output/";
  const idx = rawPath.indexOf(marker);
  if (idx !== -1) {
    return "/media/" + rawPath.slice(idx + marker.length);
  }
  return rawPath;
}
