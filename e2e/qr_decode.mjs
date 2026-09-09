/**
 * Decode a QR matrix that was scraped out of the rendered page.
 *
 * Reads {"size": n, "dark": [[row, col], ...]} on stdin and prints the decoded
 * text on stdout. Used by the share-menu end-to-end test so the assertion is
 * "a scanner reads this as the room URL" rather than "an <svg> exists".
 *
 * That distinction matters: the component builds its path with `M{col} {row}`
 * while indexing `matrix[row][col]`, so swapping the two would emit a
 * transposed symbol. It would still look like a QR code, and the three finder
 * patterns would still land in three corners, because transposition maps that
 * set onto itself. Only decoding catches it.
 */

import { readFileSync } from 'node:fs';
// Imported by path rather than as a bare specifier: Node resolves ESM imports
// relative to this file, and `qr` is installed under frontend/node_modules.
import decodeQR from '../frontend/node_modules/qr/decode.js';

const MODULE_PX = 8; // enough resolution for the decoder to lock on

const { size, dark } = JSON.parse(readFileSync(0, 'utf8'));

const width = size * MODULE_PX;
const data = new Uint8Array(width * width * 4).fill(255);

for (const [row, col] of dark) {
  for (let dy = 0; dy < MODULE_PX; dy += 1) {
    for (let dx = 0; dx < MODULE_PX; dx += 1) {
      const i = ((row * MODULE_PX + dy) * width + (col * MODULE_PX + dx)) * 4;
      data[i] = 0;
      data[i + 1] = 0;
      data[i + 2] = 0;
    }
  }
}

process.stdout.write(decodeQR({ width, height: width, data }));
