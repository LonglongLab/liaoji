/**
 * 在一块画布上循环播放 wxgf 表情（PSU-2.146）。
 *
 * **每帧时长还没读出来**：没有任何公开实现读出过（`chatlog` 的 `wxgf.go` 写死
 * 30 fps），头部与帧前表里的候选字段留给 T5 对照。在那之前固定 100 ms 一帧——
 * 快慢可能与微信里不同，但动的是对的帧、颜色是对的。
 */

import { decodeWxgf, type DecodedWxgf } from "./decode";

export const STICKER_FRAME_MS = 100;

export interface StickerPlayback {
  frameCount: number;
  /** false = 这个浏览器只解得出第一帧，界面要说一声。 */
  complete: boolean;
  decoder: DecodedWxgf["decoder"];
  stop: () => void;
}

export const playWxgfSticker = async (
  canvas: HTMLCanvasElement,
  blob: Blob,
  frameMs = STICKER_FRAME_MS,
): Promise<StickerPlayback> => {
  const decoded = await decodeWxgf(new Uint8Array(await blob.arrayBuffer()), { allFrames: true });
  canvas.width = decoded.width;
  canvas.height = decoded.height;
  const context = canvas.getContext("2d");
  if (context === null) throw new Error("画布不可用");
  const { frames } = decoded;
  let index = 0;
  const draw = (): void => {
    const frame = frames[index % frames.length];
    if (frame === undefined) return;
    context.clearRect(0, 0, canvas.width, canvas.height);
    context.drawImage(frame, 0, 0);
    index += 1;
  };
  draw();
  const timer = frames.length > 1 ? setInterval(draw, frameMs) : undefined;
  return {
    frameCount: frames.length,
    complete: decoded.complete,
    decoder: decoded.decoder,
    stop: () => {
      clearInterval(timer);
    },
  };
};
