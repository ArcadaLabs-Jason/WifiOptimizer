// The Send report preview, as text the controller can step through. Kept
// apart from the panel (no imports) so regress.py can run it under node.

// A block is what one D-pad step moves by. Twelve lines of up to 300
// characters did not fit the box, so a block is at most this many lines and
// about this many characters.
export const BLOCK_LINES = 6;
export const BLOCK_CHARS = 600;

// Control characters other than tab would print as nothing, or move the
// text around, so they are shown escaped, as JSON would show them.
function visible(text: string): string {
  return text.replace(
    /[\u0000-\u0008\u000b-\u001f\u007f]/g,
    (c) => "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0"),
  );
}

function keyText(key: string): string {
  return /^[\w.-]+$/.test(key) ? key : JSON.stringify(key);
}

function walk(value: unknown, indent: string, head: string, out: string[]): void {
  if (typeof value === "string") {
    const parts = value.replace(/\r\n?/g, "\n").replace(/\n+$/, "").split("\n");
    if (parts.length <= 1) {
      out.push(indent + head + JSON.stringify(value));
      return;
    }
    // Command output (iw, os-release) as the lines it has, not one line of
    // \n and \t escapes.
    out.push(indent + (head.trimEnd() || "|"));
    for (const part of parts) out.push(indent + "  " + visible(part.replace(/\t/g, "    ")));
    return;
  }
  if (Array.isArray(value)) {
    if (!value.length) {
      out.push(indent + head + "[]");
      return;
    }
    if (head) out.push(indent + head.trimEnd());
    const inner = head ? indent + "  " : indent;
    for (const item of value) walk(item, inner, "- ", out);
    return;
  }
  if (value && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    if (!entries.length) {
      out.push(indent + head + "{}");
      return;
    }
    if (head) out.push(indent + head.trimEnd());
    const inner = head ? indent + "  " : indent;
    for (const [key, item] of entries) walk(item, inner, keyText(key) + ": ", out);
    return;
  }
  // Numbers, booleans and null, as JSON writes them.
  out.push(indent + head + String(JSON.stringify(value) ?? value));
}

// Every value in the report, one line each, with nesting shown by indent.
export function previewLines(payload: unknown): string[] {
  const out: string[] = [];
  walk(payload, "", "", out);
  return out;
}

// The lines in blocks of at most BLOCK_LINES lines and BLOCK_CHARS
// characters. A line longer than that is cut across blocks, so no block is
// taller than the box.
export function previewBlocks(lines: string[]): string[] {
  const pieces: string[] = [];
  for (const line of lines) {
    if (line.length <= BLOCK_CHARS) pieces.push(line);
    else for (let i = 0; i < line.length; i += BLOCK_CHARS) pieces.push(line.slice(i, i + BLOCK_CHARS));
  }
  const blocks: string[] = [];
  let current: string[] = [];
  let chars = 0;
  for (const piece of pieces) {
    if (current.length && (current.length >= BLOCK_LINES || chars + piece.length > BLOCK_CHARS)) {
      blocks.push(current.join("\n"));
      current = [];
      chars = 0;
    }
    current.push(piece);
    chars += piece.length;
  }
  if (current.length) blocks.push(current.join("\n"));
  return blocks;
}
