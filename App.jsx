import { useState, useRef, useCallback, useEffect } from "react";
import ReactMarkdown from 'react-markdown';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import 'katex/dist/katex.min.css';
const API = import.meta.env.VITE_API_URL || "/api";

// The backend renders page images at 150 DPI but returns text coordinates
// in PDF points (72 DPI). This ratio converts points to image pixels.
const DPI = 150;
const DPI_RATIO = DPI / 72;

// ── Types ─────────────────────────────────────────────────────────────────────
const FONTS = [
  "Helvetica", "Helvetica Bold", "Helvetica Oblique",
  "Times Roman", "Times Bold", "Times Italic",
  "Courier", "Courier Bold",
];

const FONT_CSS_MAP = {
  "Helvetica": "Helvetica, Arial, sans-serif",
  "Helvetica Bold": "Helvetica, Arial, sans-serif",
  "Helvetica Oblique": "Helvetica, Arial, sans-serif",
  "Times Roman": "Times New Roman, Times, serif",
  "Times Bold": "Times New Roman, Times, serif",
  "Times Italic": "Times New Roman, Times, serif",
  "Courier": "Courier New, Courier, monospace",
  "Courier Bold": "Courier New, Courier, monospace",
};

// Measure text width using a canvas and return a scaled-down fontSize if it exceeds maxWidth
function fitFontSize(text, fontFamily, baseFontSize, maxWidth, fontWeight = "normal", fontStyle = "normal") {
  if (!text || maxWidth <= 0 || text.includes('\n')) return baseFontSize;
  try {
    const canvas = fitFontSize._c || (fitFontSize._c = document.createElement('canvas'));
    const ctx = canvas.getContext('2d');
    ctx.font = `${fontStyle} ${fontWeight} ${baseFontSize}px ${fontFamily}`;
    const measured = ctx.measureText(text).width;
    if (measured <= maxWidth) return baseFontSize;
    // Scale down proportionally, floor at 70% for legibility
    return Math.max(baseFontSize * (maxWidth / measured) * 0.98, baseFontSize * 0.70);
  } catch {
    return baseFontSize;
  }
}

// Convert hex color to rgba with custom opacity to make background semi-transparent
function getRgbaColor(hex, opacity = 0.8) {
  if (!hex || hex === "transparent") return "transparent";
  if (hex.startsWith("#")) {
    const cleanHex = hex.replace("#", "");
    if (cleanHex.length === 3) {
      const r = parseInt(cleanHex[0] + cleanHex[0], 16);
      const g = parseInt(cleanHex[1] + cleanHex[1], 16);
      const b = parseInt(cleanHex[2] + cleanHex[2], 16);
      return `rgba(${r}, ${g}, ${b}, ${opacity})`;
    }
    const r = parseInt(cleanHex.slice(0, 2), 16);
    const g = parseInt(cleanHex.slice(2, 4), 16);
    const b = parseInt(cleanHex.slice(4, 6), 16);
    return `rgba(${r}, ${g}, ${b}, ${opacity})`;
  }
  return hex;
}

// Mirror of the backend's _ensure_readable_contrast: when text is redrawn in a
// substitute font (global font change), very light colours that were only
// legible thanks to a heavy original face are darkened just enough to read.
function ensureReadableContrast(hex, bgHex, minDelta = 0.35) {
  const parse = (h) => {
    if (!h || !h.startsWith("#")) return [1, 1, 1];
    const c = h.replace("#", "");
    const full = c.length === 3 ? c.split("").map((ch) => ch + ch).join("") : c;
    return [0, 2, 4].map((i) => parseInt(full.slice(i, i + 2), 16) / 255);
  };
  const lum = (c) => 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2];
  const txt = parse(hex);
  const bg = parse(bgHex || "#ffffff");
  const tl = lum(txt), bl = lum(bg);
  if (Math.abs(tl - bl) >= minDelta) return hex;
  let out;
  if (bl >= 0.5) {
    if (tl <= 1e-6) return hex;
    const scale = Math.max(bl - minDelta, 0) / tl;
    out = txt.map((c) => Math.max(0, Math.min(1, c * scale)));
  } else {
    const target = Math.min(bl + minDelta, 1);
    const blend = tl >= 1 ? 0 : Math.max(0, Math.min(1, (target - tl) / (1 - tl)));
    out = txt.map((c) => c + (1 - c) * blend);
  }
  return "#" + out.map((c) => Math.round(c * 255).toString(16).padStart(2, "0")).join("");
}

// ── Components ────────────────────────────────────────────────────────────────

function Spinner() {
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 12 }}>
      <div style={{
        width: 40, height: 40, borderRadius: "50%",
        border: "3px solid #1e293b", borderTopColor: "#6366f1",
        animation: "spin 0.8s linear infinite"
      }} />
      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  );
}

function ToolBar({ activeTool, setActiveTool, onSave, saving, hasDoc, onUndo, onRedo, canUndo, canRedo, documentFont, setDocumentFont }) {
  const tools = [
    { id: "select", icon: "[]", label: "Select Region" },
    { id: "edit", icon: "T", label: "Edit Text" },
  ];

  return (
    <div style={{
      display: "flex", alignItems: "center", gap: 8,
      padding: "10px 16px",
      background: "#0f172a",
      borderBottom: "1px solid #1e293b",
    }}>
      <span style={{ color: "#6366f1", fontWeight: 700, fontSize: 15, marginRight: 8, letterSpacing: "-0.5px" }}>
        ◈ PDFEdit
      </span>
      <div style={{ width: 1, height: 24, background: "#1e293b", margin: "0 4px" }} />
      {tools.map(t => (
        <button
          key={t.id}
          title={t.label}
          onClick={() => setActiveTool(t.id)}
          style={{
            background: activeTool === t.id ? "#6366f1" : "#1e293b",
            color: "#f8fafc",
            border: "none", borderRadius: 6, padding: "6px 14px",
            cursor: "pointer", fontSize: 13, fontWeight: 600,
            transition: "background 0.15s",
          }}
        >
          {t.icon} {t.label}
        </button>
      ))}

      {hasDoc && (
        <>
          <div style={{ width: 1, height: 24, background: "#1e293b", margin: "0 4px" }} />
          <button
            onClick={onUndo}
            disabled={!canUndo}
            title="Undo"
            style={{
              background: "none",
              border: "none",
              color: canUndo ? "#f8fafc" : "#475569",
              cursor: canUndo ? "pointer" : "not-allowed",
              fontSize: 13,
              fontWeight: 600,
              padding: "6px 10px",
              borderRadius: 4,
            }}
          >
            ↶ Undo
          </button>
          <button
            onClick={onRedo}
            disabled={!canRedo}
            title="Redo"
            style={{
              background: "none",
              border: "none",
              color: canRedo ? "#f8fafc" : "#475569",
              cursor: canRedo ? "pointer" : "not-allowed",
              fontSize: 13,
              fontWeight: 600,
              padding: "6px 10px",
              borderRadius: 4,
            }}
          >
            ↷ Redo
          </button>
        </>
      )}

      <div style={{ flex: 1 }} />

      {hasDoc && (
        <div style={{ display: "flex", alignItems: "center", gap: 6, marginRight: 12 }}>
          <span style={{ fontSize: 12, color: "#94a3b8" }}>Doc Font:</span>
          <select
            value={documentFont}
            onChange={e => setDocumentFont(e.target.value)}
            style={{
              background: "#1e293b",
              color: "#f8fafc",
              border: "1px solid #334155",
              borderRadius: 6,
              padding: "5px 10px",
              fontSize: 12,
              cursor: "pointer",
            }}
          >
            <option value="Original">Original Fonts</option>
            <option value="Helvetica">Helvetica (Sans-Serif)</option>
            <option value="Times Roman">Times New Roman (Serif)</option>
            <option value="Courier">Courier New (Monospace)</option>
          </select>
        </div>
      )}

      {hasDoc && (
        <button
          onClick={onSave}
          disabled={saving}
          style={{
            background: saving ? "#334155" : "#10b981",
            color: "#fff", border: "none", borderRadius: 6,
            padding: "7px 18px", cursor: saving ? "not-allowed" : "pointer",
            fontWeight: 700, fontSize: 13,
          }}
        >
          {saving ? "Saving..." : "Download Edited PDF"}
        </button>
      )}
    </div>
  );
}

function PropertiesPanel({
  selectedBlock,
  onUpdate,
  editMode,
  setEditMode,
  aiReplacementText,
  setAiReplacementText,
  aiPadding,
  setAiPadding,
  aiEditProvider,
  setAiEditProvider,
  aiEditApiKey,
  setAiEditApiKey,
  onAiReplace,
  aiLoading,
  aiPreview,
  aiError,
  aiMessage,
}) {
  if (!selectedBlock) {
    return (
      <div style={{ padding: 20, color: "#64748b", fontSize: 13, textAlign: "center" }}>
        <div style={{ fontSize: 28, marginBottom: 8 }}>T</div>
        Click any text block to edit it,
        <br />or use <b>Select Region</b> to drag a rectangle.
        <br /><br />
        <span style={{ fontSize: 11 }}>Use the floating toolbar above the selected block for text, font, size, and color edits.</span>
      </div>
    );
  }

  const { text, font_name, font_size, color } = selectedBlock;

  return (
    <div style={{ padding: 16, display: "flex", flexDirection: "column", gap: 14 }}>
      <div style={{ color: "#94a3b8", fontSize: 11, fontWeight: 700, letterSpacing: 1, textTransform: "uppercase" }}>
        Block Info
      </div>

      {/* Block metadata (read-only summary) */}
      <div style={{ background: "#1e293b", borderRadius: 8, padding: 12, display: "flex", flexDirection: "column", gap: 8 }}>
        <div style={{ fontSize: 11, color: "#94a3b8" }}>
          <span style={{ fontWeight: 700 }}>Font:</span> {font_name}
        </div>
        <div style={{ fontSize: 11, color: "#94a3b8" }}>
          <span style={{ fontWeight: 700 }}>Size:</span> {font_size?.toFixed(1)}pt
        </div>
        <div style={{ fontSize: 11, color: "#94a3b8", display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ fontWeight: 700 }}>Color:</span>
          <div style={{ width: 14, height: 14, borderRadius: 3, background: color, border: "1px solid #475569" }} />
          <span>{color}</span>
        </div>
        <div style={{ fontSize: 11, color: "#64748b", fontStyle: "italic", borderTop: "1px solid #334155", paddingTop: 8, marginTop: 4 }}>
          ✎ Use the floating toolbar on the page to edit text, font, and styling.
        </div>
      </div>

      <div style={{ width: "100%", height: 1, background: "#1e293b" }} />

      <div style={{ color: "#94a3b8", fontSize: 11, fontWeight: 700, letterSpacing: 1, textTransform: "uppercase" }}>
        AI Edit
      </div>

      {/* AI Edit controls */}
      <label style={labelStyle}>
        <span>AI Provider</span>
        <select
          value={aiEditProvider}
          onChange={e => setAiEditProvider(e.target.value)}
          style={inputStyle}
        >
          <option value="gemini">Gemini</option>
          <option value="replicate">Replicate (InstructPix2Pix)</option>
        </select>
      </label>
      <label style={labelStyle}>
        <span>API Key</span>
        <input
          type="password"
          value={aiEditApiKey}
          onChange={e => setAiEditApiKey(e.target.value)}
          placeholder="Stored locally in this browser"
          style={inputStyle}
        />
      </label>

      <label style={labelStyle}>
        <span>Original Text</span>
        <textarea
          value={text}
          readOnly
          style={{ ...inputStyle, minHeight: 54, resize: "vertical", lineHeight: 1.4, color: "#94a3b8" }}
        />
      </label>

      <label style={labelStyle}>
        <span>Replacement Text</span>
        <textarea
          value={aiReplacementText}
          onChange={e => setAiReplacementText(e.target.value)}
          placeholder="Text AI should render into this region"
          style={{ ...inputStyle, minHeight: 70, resize: "vertical", lineHeight: 1.4 }}
        />
      </label>

      <button
        onClick={onAiReplace}
        disabled={aiLoading || !aiReplacementText.trim()}
        style={{
          background: aiLoading || !aiReplacementText.trim() ? "#334155" : "#10b981",
          color: "#fff",
          border: "none",
          borderRadius: 6,
          padding: "9px 12px",
          cursor: aiLoading || !aiReplacementText.trim() ? "not-allowed" : "pointer",
          fontWeight: 700,
          fontSize: 13,
        }}
      >
        {aiLoading ? "Preparing..." : "AI Replace"}
      </button>

      {aiError && (
        <div style={{ background: "#450a0a", color: "#fca5a5", borderRadius: 6, padding: "8px 10px", fontSize: 12 }}>
          {aiError}
        </div>
      )}

      {aiMessage && (
        <div style={{ background: "#052e16", color: "#86efac", borderRadius: 6, padding: "8px 10px", fontSize: 12 }}>
          {aiMessage}
        </div>
      )}

      {aiPreview && (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <div style={{ color: "#94a3b8", fontSize: 11, fontWeight: 700, textTransform: "uppercase" }}>
            Crop Preview
          </div>
          <img
            src={`data:image/png;base64,${aiPreview.crop_image_b64}`}
            alt="AI edit crop preview"
            style={{ width: "100%", background: "#fff", borderRadius: 4, border: "1px solid #334155" }}
          />
          <button
            onClick={() => {
              onUpdate({
                text: aiReplacementText,
                crop_image_b64: aiPreview.crop_image_b64,
                edit_id: aiPreview.edit_id,
                is_ai_edit: true
              });
            }}
            style={{
              background: "#10b981",
              color: "#fff",
              border: "none",
              borderRadius: 6,
              padding: "9px 12px",
              cursor: "pointer",
              fontWeight: 700,
              fontSize: 13,
              marginTop: 4,
              width: "100%",
            }}
          >
            Apply AI Edit
          </button>
        </div>
      )}

      {selectedBlock.is_ocr && (
        <div style={{
          background: "#1e293b", borderRadius: 6, padding: "8px 10px",
          fontSize: 11, color: "#f59e0b", display: "flex", gap: 6,
        }}>
          OCR-detected text - check for accuracy
        </div>
      )}
    </div>
  );
}

const labelStyle = {
  display: "flex", flexDirection: "column", gap: 5,
  fontSize: 12, color: "#94a3b8", fontWeight: 600,
};
const inputStyle = {
  background: "#1e293b", color: "#f8fafc", border: "1px solid #334155",
  borderRadius: 6, padding: "7px 10px", fontSize: 13, width: "100%",
  boxSizing: "border-box",
};

// ── PDF Page Canvas ───────────────────────────────────────────────────────────

function PDFPage({ pageInfo, scale, activeTool, selectedIds, onSelectBlock, onRectSelect, editedBlocks, selectedBlock, onUpdateBlock, documentFont }) {
  const canvasRef = useRef(null);
  const [hoverId, setHoverId] = useState(null);
  const [dragStart, setDragStart] = useState(null);  // { x, y } in pixels relative to container
  const [dragEnd, setDragEnd] = useState(null);

  // Merge original blocks with any local edits
  const blocks = pageInfo.text_blocks.map(b => ({
    ...b,
    ...(editedBlocks[b.id] || {}),
  }));

  // Container is sized to the actual image pixel dimensions (points x DPI_RATIO),
  // then scaled by the user-zoom factor.
  const pw = pageInfo.width * DPI_RATIO * scale;
  const ph = pageInfo.height * DPI_RATIO * scale;

  // Combined scale factor: points to display pixels
  const totalScale = DPI_RATIO * scale;

  // Convert a mouse event to position relative to the container
  const getRelPos = useCallback((e) => {
    const rect = canvasRef.current?.getBoundingClientRect();
    if (!rect) return { x: 0, y: 0 };
    return { x: e.clientX - rect.left, y: e.clientY - rect.top };
  }, []);

  // Convert pixel position to PDF points
  const toPoints = useCallback((pos) => ({
    x: pos.x / totalScale,
    y: pos.y / totalScale,
  }), [totalScale]);

  const handleMouseDown = useCallback((e) => {
    if (activeTool !== "select") return;
    // Only start drag from left button on the background (not on a block)
    if (e.button !== 0) return;
    const pos = getRelPos(e);
    setDragStart(pos);
    setDragEnd(pos);
  }, [activeTool, getRelPos]);

  const handleMouseMove = useCallback((e) => {
    if (dragStart) {
      // Dragging a selection rectangle
      setDragEnd(getRelPos(e));
      return;
    }
    // Hover detection for edit mode
    if (activeTool === "edit") {
      const pt = toPoints(getRelPos(e));
      let found = null;
      for (let i = blocks.length - 1; i >= 0; i--) {
        const b = blocks[i];
        if (pt.x >= b.x - 2 && pt.x <= b.x + b.width + 6 &&
          pt.y >= b.y - 2 && pt.y <= b.y + b.height + 6) {
          found = b;
          break;
        }
      }
      setHoverId(found?.id || null);
    }
  }, [dragStart, activeTool, blocks, getRelPos, toPoints]);

  const handleMouseUp = useCallback((e) => {
    if (!dragStart) return;
    const end = getRelPos(e);

    // Calculate rectangle in PDF points
    const p1 = toPoints(dragStart);
    const p2 = toPoints(end);
    const rectLeft = Math.min(p1.x, p2.x);
    const rectTop = Math.min(p1.y, p2.y);
    const rectRight = Math.max(p1.x, p2.x);
    const rectBottom = Math.max(p1.y, p2.y);
    const rectW = rectRight - rectLeft;
    const rectH = rectBottom - rectTop;

    // If it's just a click (tiny rect), treat as single-block click
    if (rectW < 3 && rectH < 3) {
      const pt = toPoints(end);
      let found = null;
      for (let i = blocks.length - 1; i >= 0; i--) {
        const b = blocks[i];
        if (pt.x >= b.x - 2 && pt.x <= b.x + b.width + 6 &&
          pt.y >= b.y - 2 && pt.y <= b.y + b.height + 6) {
          found = b;
          break;
        }
      }
      onSelectBlock(found);
    } else {
      // Find all blocks that intersect the selection rectangle
      const hits = blocks.filter(b => {
        const bRight = b.x + b.width;
        const bBottom = b.y + b.height;
        return b.x < rectRight && bRight > rectLeft &&
          b.y < rectBottom && bBottom > rectTop;
      });
      if (hits.length > 0) {
        onRectSelect(hits);
      } else {
        onSelectBlock(null);
      }
    }

    setDragStart(null);
    setDragEnd(null);
  }, [dragStart, blocks, getRelPos, toPoints, onSelectBlock, onRectSelect]);

  // Only deselect when clicking the overlay background, not a text block
  const handleOverlayClick = useCallback((e) => {
    if (activeTool !== "edit") return;
    if (e.target !== e.currentTarget) return;
    onSelectBlock(null);
  }, [activeTool, onSelectBlock]);

  // Selection rectangle in pixels
  const selRect = dragStart && dragEnd ? {
    left: Math.min(dragStart.x, dragEnd.x),
    top: Math.min(dragStart.y, dragEnd.y),
    width: Math.abs(dragEnd.x - dragStart.x),
    height: Math.abs(dragEnd.y - dragStart.y),
  } : null;

  const getCursor = () => {
    if (activeTool === "select") return dragStart ? "crosshair" : "crosshair";
    if (activeTool === "edit") return hoverId ? "text" : "default";
    return "default";
  };

  const activeSelectedBlock = blocks.find(b => selectedIds.has(b.id));
  const hasSelectedBlock = selectedBlock && activeSelectedBlock && selectedBlock.id === activeSelectedBlock.id;
  const isBold = activeSelectedBlock ? (activeSelectedBlock.font_name?.toLowerCase().includes("bold") || activeSelectedBlock.bold) : false;
  const isItalic = activeSelectedBlock ? (
    activeSelectedBlock.font_name?.toLowerCase().includes("italic") ||
    activeSelectedBlock.font_name?.toLowerCase().includes("oblique") ||
    activeSelectedBlock.italic
  ) : false;

  const toggleBold = () => {
    if (!activeSelectedBlock) return;
    let newFont = activeSelectedBlock.font_name || "Helvetica";
    if (newFont.toLowerCase().includes("bold")) {
      newFont = newFont.replace(" Bold", "").replace(" bold", "").replace("Bold", "");
    } else {
      if (newFont === "Helvetica") newFont = "Helvetica Bold";
      else if (newFont === "Times Roman") newFont = "Times Bold";
      else if (newFont === "Courier") newFont = "Courier Bold";
      else newFont = newFont + " Bold";
    }
    onUpdateBlock({ font_name: newFont, bold: !isBold });
  };

  const toggleItalic = () => {
    if (!activeSelectedBlock) return;
    let newFont = activeSelectedBlock.font_name || "Helvetica";
    if (newFont.toLowerCase().includes("italic")) {
      newFont = newFont.replace(" Italic", "").replace(" italic", "").replace("Italic", "");
    } else if (newFont.toLowerCase().includes("oblique")) {
      newFont = newFont.replace(" Oblique", "").replace(" oblique", "").replace("Oblique", "");
    } else {
      if (newFont === "Helvetica") newFont = "Helvetica Oblique";
      else if (newFont === "Times Roman") newFont = "Times Italic";
      else newFont = newFont + " Italic";
    }
    onUpdateBlock({ font_name: newFont, italic: !isItalic });
  };

  let popTop = 0;
  let popLeft = 0;
  const popoverWidth = 420;
  const popoverHeight = 135;

  if (hasSelectedBlock) {
    const blkLeft = activeSelectedBlock.x * totalScale;
    const blkTop = activeSelectedBlock.y * totalScale;
    const blkW = Math.max(activeSelectedBlock.width + 4, 8) * totalScale;
    const blkH = Math.max(activeSelectedBlock.height, 10) * totalScale;

    const showBelow = blkTop < popoverHeight;
    popTop = showBelow ? (blkTop + blkH + 8) : (blkTop - popoverHeight - 8);
    let targetLeft = blkLeft + (blkW - popoverWidth) / 2;
    targetLeft = Math.max(blkLeft - 50, targetLeft);
    popLeft = Math.max(8, Math.min(pw - popoverWidth - 8, targetLeft));
  }

  const showPopover = hasSelectedBlock && !dragStart;

  return (
    <div style={{
      position: "relative", width: pw, height: ph,
      marginBottom: 24, boxShadow: "0 4px 32px rgba(0,0,0,0.5)",
      borderRadius: 2, overflow: "visible",
    }}>
      {/* Page image background */}
      <img
        src={`data:image/png;base64,${pageInfo.image_b64}`}
        style={{ position: "absolute", top: 0, left: 0, width: pw, height: ph, display: "block" }}
        alt={`Page ${pageInfo.page_number + 1}`}
        draggable={false}
      />

      {/* Interactive overlay layer */}
      <div
        ref={canvasRef}
        onClick={handleOverlayClick}
        onMouseDown={handleMouseDown}
        onMouseMove={handleMouseMove}
        onMouseUp={handleMouseUp}
        onMouseLeave={() => { setHoverId(null); if (dragStart) { setDragStart(null); setDragEnd(null); } }}
        style={{
          position: "absolute", top: 0, left: 0, width: pw, height: ph,
          cursor: getCursor(),
          userSelect: "none",
        }}
      >
        {/* Invisible hit-target overlays for each text block */}
        {blocks.map(block => {
          const isSelected = selectedIds.has(block.id);
          const isHovered = block.id === hoverId;
          const showText = documentFont !== "Original" || editedBlocks[block.id];
          // When the box is an opaque cover over original text it must hug the
          // exact bbox — any padding/minimum size makes it swallow nearby table
          // borders. The roomier dimensions are only for invisible hit targets.
          const boxW = showText
            ? Math.max(block.width, 2) * totalScale
            : Math.max(block.width + 4, 8) * totalScale;
          const fFamily = FONT_CSS_MAP[documentFont] || FONT_CSS_MAP[block.font_name] || "Helvetica";
          const fWeight = (block.font_name?.toLowerCase().includes("bold") || block.bold) ? "bold" : "normal";
          const fStyle = (block.font_name?.toLowerCase().includes("italic") || block.font_name?.toLowerCase().includes("oblique") || block.italic) ? "italic" : "normal";
          const baseFontSize = block.font_size * totalScale;
          // Auto-scale font to fit within the bounding box width
          const displayFontSize = (showText && block.text && !block.text.includes('\n'))
            ? fitFontSize(block.text, fFamily, baseFontSize, boxW - 4, fWeight, fStyle)
            : baseFontSize;

          return (
            <div
              key={block.id}
              style={{
                position: "absolute",
                left: block.x * totalScale,
                top: block.y * totalScale,
                width: boxW,
                minWidth: showText ? 0 : 24,
                height: showText
                  ? Math.max(block.height, 4) * totalScale
                  : Math.max(block.height, 10) * totalScale,
                minHeight: showText ? 0 : 16,
                color: showText
                  ? (block.is_ai_edit
                    ? "transparent"
                    : ensureReadableContrast(block.color, block.background_color))
                  : "transparent",
                fontFamily: fFamily,
                fontWeight: fWeight,
                fontStyle: fStyle,
                fontSize: displayFontSize,
                whiteSpace: block.text?.includes("\n") ? "pre-wrap" : "nowrap",
                lineHeight: 1.2,
                pointerEvents: "auto",
                cursor: activeTool === "edit" ? "text" : "pointer",
                outline: isSelected
                  ? "2px solid #6366f1"
                  : isHovered
                    ? "1px dashed rgba(148,163,184,0.6)"
                    : "none",
                outlineOffset: 1,
                borderRadius: 2,
                background: showText
                  ? (block.is_ai_edit
                    ? `url(data:image/png;base64,${block.crop_image_b64}) no-repeat center/cover`
                    : getRgbaColor(block.background_color || "#ffffff", 1.0))
                  : isSelected
                    ? "rgba(99,102,241,0.3)"
                    : isHovered
                      ? "rgba(148,163,184,0.2)"
                      : "transparent",
                boxSizing: "border-box",
                transition: "outline 0.1s, background 0.1s",
                padding: "0 2px",
              }}
              onMouseDown={(e) => e.stopPropagation()}
              onClick={(e) => {
                e.preventDefault();
                e.stopPropagation();
                onSelectBlock(block);
              }}
              onMouseEnter={() => { if (!dragStart) setHoverId(block.id); }}
              onMouseLeave={() => { if (!dragStart) setHoverId(null); }}
            >
              {showText ? block.text : null}
            </div>
          );
        })}

        {/* Rubber-band selection rectangle */}
        {selRect && selRect.width > 2 && selRect.height > 2 && (
          <div style={{
            position: "absolute",
            left: selRect.left,
            top: selRect.top,
            width: selRect.width,
            height: selRect.height,
            border: "2px dashed #6366f1",
            background: "rgba(99,102,241,0.08)",
            borderRadius: 2,
            pointerEvents: "none",
          }} />
        )}
      </div>

      {/* Floating inline editing toolbar */}
      {showPopover && (
        <div style={{
          position: "absolute",
          top: popTop,
          left: popLeft,
          width: popoverWidth,
          background: "rgba(15, 23, 42, 0.95)",
          backdropFilter: "blur(12px)",
          border: "1px solid #334155",
          borderRadius: 8,
          boxShadow: "0 10px 25px -5px rgba(0,0,0,0.6), 0 8px 10px -6px rgba(0,0,0,0.6)",
          padding: "10px 12px",
          zIndex: 40,
          display: "flex",
          flexDirection: "column",
          gap: 8,
          pointerEvents: "auto",
        }}>
          {/* Format toolbar row */}
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <select
              value={activeSelectedBlock.font_name || "Helvetica"}
              onChange={e => onUpdateBlock({ font_name: e.target.value })}
              style={{
                background: "#1e293b", color: "#f8fafc", border: "1px solid #334155",
                borderRadius: 4, padding: "3px 6px", fontSize: 11,
              }}
            >
              {FONTS.map(f => <option key={f} value={f}>{f}</option>)}
            </select>

            <div style={{ display: "flex", alignItems: "center", background: "#1e293b", border: "1px solid #334155", borderRadius: 4 }}>
              <button
                onClick={() => onUpdateBlock({ font_size: Math.max(6, (activeSelectedBlock.font_size || 12) - 1) })}
                style={{ background: "none", border: "none", color: "#f8fafc", padding: "3px 8px", cursor: "pointer", fontSize: 11 }}
                title="Decrease font size"
              >
                A-
              </button>
              <span style={{ fontSize: 11, color: "#cbd5e1", minWidth: 20, textAlign: "center" }}>
                {Math.round(activeSelectedBlock.font_size || 12)}
              </span>
              <button
                onClick={() => onUpdateBlock({ font_size: Math.min(72, (activeSelectedBlock.font_size || 12) + 1) })}
                style={{ background: "none", border: "none", color: "#f8fafc", padding: "3px 8px", cursor: "pointer", fontSize: 11 }}
                title="Increase font size"
              >
                A+
              </button>
            </div>

            <div style={{ width: 1, height: 16, background: "#334155" }} />

            <button
              onClick={toggleBold}
              style={{
                background: isBold ? "#6366f1" : "transparent",
                color: "#fff",
                border: "1px solid #334155",
                borderRadius: 4,
                width: 24, height: 24,
                cursor: "pointer",
                fontWeight: "bold",
                fontSize: 11,
              }}
              title="Bold"
            >
              B
            </button>

            <button
              onClick={toggleItalic}
              style={{
                background: isItalic ? "#6366f1" : "transparent",
                color: "#fff",
                border: "1px solid #334155",
                borderRadius: 4,
                width: 24, height: 24,
                cursor: "pointer",
                fontStyle: "italic",
                fontSize: 11,
              }}
              title="Italic"
            >
              I
            </button>

            <div style={{ position: "relative", width: 24, height: 24 }}>
              <input
                type="color"
                value={activeSelectedBlock.color || "#000000"}
                onChange={e => onUpdateBlock({ color: e.target.value })}
                style={{
                  position: "absolute", top: 0, left: 0, width: "100%", height: "100%",
                  border: "none", padding: 0, opacity: 0, cursor: "pointer",
                }}
              />
              <div style={{
                width: 20, height: 20, borderRadius: "50%",
                background: activeSelectedBlock.color || "#000000",
                border: "1px solid #475569",
                margin: 2,
              }} title="Text Color" />
            </div>

            <div style={{ position: "relative", width: 24, height: 24 }}>
              <input
                type="color"
                value={activeSelectedBlock.background_color || "#ffffff"}
                onChange={e => onUpdateBlock({ background_color: e.target.value })}
                style={{
                  position: "absolute", top: 0, left: 0, width: "100%", height: "100%",
                  border: "none", padding: 0, opacity: 0, cursor: "pointer",
                }}
              />
              <div style={{
                width: 20, height: 20,
                background: activeSelectedBlock.background_color || "#ffffff",
                border: "1px solid #475569",
                margin: 2,
              }} title="Background Color (covers original text)" />
            </div>

            <div style={{ flex: 1 }} />

            <button
              onClick={() => onSelectBlock(null)}
              style={{
                background: "transparent", border: "none", color: "#94a3b8",
                cursor: "pointer", fontSize: 14, fontWeight: "bold",
              }}
              title="Done"
            >
              ✕
            </button>
          </div>

          <div style={{ display: "flex", gap: 6 }}>
            <textarea
              value={activeSelectedBlock.text || ""}
              onChange={e => onUpdateBlock({ text: e.target.value }, true)}
              style={{
                flex: 1,
                background: "#1e293b",
                color: "#f8fafc",
                border: "1px solid #334155",
                borderRadius: 4,
                padding: "6px 8px",
                fontSize: 12,
                minHeight: 36,
                maxHeight: 120,
                resize: "vertical",
                fontFamily: "inherit",
              }}
              placeholder="Edit text content..."
              autoFocus
            />
          </div>
        </div>
      )}

      {/* Page number badge */}
      <div style={{
        position: "absolute", bottom: -20, left: 0, right: 0,
        textAlign: "center", fontSize: 11, color: "#64748b",
      }}>
        Page {pageInfo.page_number + 1}
        {pageInfo.is_scanned && <span style={{ color: "#f59e0b", marginLeft: 6 }}>OCR</span>}
      </div>
    </div>
  );
}

// ── Main App ──────────────────────────────────────────────────────────────────


// ── Scan edit-mode selection modal ────────────────────────────────────────────
function ScanModeModal({ onReconstruct, onAiImage, mineruBackend, setMineruBackend, mineruEffort, setMineruEffort, nvidiaApiKey, setNvidiaApiKey }) {
  const card = {
    flex: 1, background: "#0f172a", border: "1px solid #1e293b",
    borderRadius: 12, padding: 20, textAlign: "left",
    color: "#f8fafc", display: "flex", flexDirection: "column", gap: 8,
  };
  return (
    <div style={{
      position: "fixed", inset: 0, background: "rgba(2,6,23,0.82)",
      display: "flex", alignItems: "center", justifyContent: "center",
      zIndex: 50, padding: 24,
    }}>
      <div style={{ width: "100%", maxWidth: 720 }}>
        <div style={{ fontSize: 18, fontWeight: 700, marginBottom: 6 }}>
          This is a scanned PDF — choose an editing mode
        </div>
        <div style={{ fontSize: 13, color: "#94a3b8", marginBottom: 18 }}>
          You can pick a different mode at any time from the left sidebar.
        </div>
        <div style={{ display: "flex", gap: 16 }}>
          <div style={card}>
            <div style={{ fontSize: 15, fontWeight: 700, color: "#818cf8" }}>Reconstruct &amp; Edit</div>
            <div style={{ fontSize: 12, color: "#cbd5e1", lineHeight: 1.5, marginBottom: 12 }}>
              Extract the page structure (headings, paragraphs, tables) into an editable document.
            </div>

            <div style={{ background: "#020617", padding: 12, borderRadius: 8, border: "1px solid #1e293b", marginBottom: 16 }}>
              <div style={{ fontSize: 11, color: "#94a3b8", marginBottom: 6, fontWeight: 700, textTransform: "uppercase" }}>OCR Engine Settings</div>
              <div style={{ display: "flex", gap: 12, marginBottom: 8 }}>
                <div style={{ flex: 1 }}>
                  <label style={{ fontSize: 11, color: "#cbd5e1", display: "block", marginBottom: 4 }}>MinerU Backend</label>
                  <select value={mineruBackend} onChange={e => setMineruBackend(e.target.value)} style={{ width: "100%", background: "#0f172a", color: "#f8fafc", border: "1px solid #334155", borderRadius: 4, padding: "4px 8px", fontSize: 12 }}>
                    <option value="pipeline">Pipeline (100% Offline / Fast)</option>
                    <option value="hybrid-http-client">Hybrid (Uses NVIDIA NIM for Charts)</option>
                    <option value="vlm-http-client">VLM Engine (Uses NVIDIA NIM for all pages)</option>
                  </select>
                </div>

                {(mineruBackend === "hybrid-http-client" || mineruBackend === "vlm-http-client") && (
                  <div style={{ flex: 1 }}>
                    <label style={{ fontSize: 11, color: "#cbd5e1", display: "block", marginBottom: 4 }}>NVIDIA NIM API Key</label>
                    <input
                      type="password"
                      placeholder="nvapi-..."
                      value={nvidiaApiKey}
                      onChange={e => setNvidiaApiKey(e.target.value)}
                      style={{ width: "100%", background: "#0f172a", color: "#f8fafc", border: "1px solid #334155", borderRadius: 4, padding: "4px 8px", fontSize: 12 }}
                    />
                  </div>
                )}
              </div>

              {(mineruBackend !== "hybrid-http-client" && mineruBackend !== "vlm-http-client") && (
                <div style={{ display: "flex", gap: 12 }}>
                  <div style={{ flex: 1 }}>
                    <label style={{ fontSize: 11, color: "#cbd5e1", display: "block", marginBottom: 4 }}>Effort Level</label>
                    <select value={mineruEffort} onChange={e => setMineruEffort(e.target.value)} style={{ width: "100%", background: "#0f172a", color: "#f8fafc", border: "1px solid #334155", borderRadius: 4, padding: "4px 8px", fontSize: 12 }}>
                      <option value="medium">Medium (Fast, Text-only)</option>
                      <option value="high">High (Includes Image/Chart Analysis)</option>
                    </select>
                  </div>
                  <div style={{ flex: 1 }} />
                </div>
              )}
            </div>

            <button onClick={onReconstruct} style={{ background: "#6366f1", color: "#fff", border: "none", borderRadius: 6, padding: "10px 16px", fontSize: 14, fontWeight: 700, cursor: "pointer", width: "100%" }}>
              Start Reconstruction
            </button>
          </div>
          <div style={{ ...card, justifyContent: "space-between" }}>
            <div>
              <div style={{ fontSize: 15, fontWeight: 700, color: "#34d399" }}>AI Image Edit</div>
              <div style={{ fontSize: 12, color: "#cbd5e1", lineHeight: 1.5 }}>
                Select a region and let AI re-render just that text while keeping
                the scan texture, fonts and surrounding content. Best when you want
                to preserve the original scanned appearance.
              </div>
            </div>
            <button onClick={onAiImage} style={{ background: "#10b981", color: "#fff", border: "none", borderRadius: 6, padding: "10px 16px", fontSize: 14, fontWeight: 700, cursor: "pointer", width: "100%", marginTop: 16 }}>
              Use AI Image Edit
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ── Reconstruct & Edit structured editor ──────────────────────────────────────
function ReconstructEditor({ sessionId, pages, filename, onBack, nvidiaApiKey, setNvidiaApiKey, mineruBackend, mineruEffort }) {
  const [doc, setDoc] = useState(null);      // ReconstructedDocument
  const [edits, setEdits] = useState({});    // elementId -> { text } | { rows }
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState(null);
  const [editingBlockId, setEditingBlockId] = useState(null);

  const runReconstruction = useCallback(async () => {
    setLoading(true); setErr(null); setDoc(null);
    try {
      const res = await fetch(`${API}/reconstruct`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: sessionId,
          page_numbers: null,
          ocr_engine: "mineru",
          nvidia_api_key: nvidiaApiKey,
          mineru_backend: mineruBackend,
          mineru_effort: mineruEffort,
        }),
      });
      let data;
      try {
        data = await res.json();
      } catch (err) {
        throw new Error(`Server error: ${res.status} - ${res.statusText}`);
      }
      if (!res.ok) throw new Error(data?.detail || "Reconstruction failed");
      setDoc(data);
    } catch (e) {
      setErr(e.message);
    } finally {
      setLoading(false);
    }
  }, [sessionId, nvidiaApiKey, mineruBackend, mineruEffort]);

  useEffect(() => {
    runReconstruction();
  }, []); // Run once on mount

  const setText = (id, text) => setEdits(prev => ({ ...prev, [id]: { text } }));
  const setCell = (id, rows, r, c, value) => {
    const next = rows.map(row => row.slice());
    next[r][c] = value;
    setEdits(prev => ({ ...prev, [id]: { rows: next } }));
  };
  const rowsFor = (el) => (edits[el.id] && edits[el.id].rows) || el.rows || [];
  const textFor = (el) => (edits[el.id] && edits[el.id].text !== undefined)
    ? edits[el.id].text : (el.text || "");

  const download = async () => {
    setSaving(true); setErr(null);
    try {
      const res = await fetch(`${API}/reconstruct/save`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, edits }),
      });
      if (!res.ok) {
        let msg = "Save failed";
        try { msg = (await res.json()).detail || msg; } catch { }
        throw new Error(msg);
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `reconstructed_${filename || "document.pdf"}`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setErr(e.message);
    } finally {
      setSaving(false);
    }
  };

  const pageImg = (pageNum) => {
    const pg = pages.find(p => p.page_number === pageNum);
    return pg ? `data:image/png;base64,${pg.image_b64}` : null;
  };

  const cellStyle = {
    border: "1px solid #334155", padding: "4px 8px", fontSize: 12,
    color: "#0f172a", background: "#fff", minWidth: 60,
  };

  return (
    <div style={{ width: "100%", maxWidth: 1200 }}>
      <div style={{
        display: "flex", alignItems: "center", justifyContent: "space-between",
        marginBottom: 16, gap: 12,
      }}>
        <button onClick={onBack} style={{
          background: "#1e293b", color: "#cbd5e1", border: "1px solid #334155",
          borderRadius: 6, padding: "7px 14px", fontSize: 13, cursor: "pointer",
        }}>&larr; Change mode</button>
        <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
          <button onClick={runReconstruction} disabled={loading} style={{
            background: loading ? "#334155" : "#10b981",
            color: "#fff", border: "none", borderRadius: 6, padding: "7px 16px",
            fontSize: 13, fontWeight: 700,
            cursor: loading ? "not-allowed" : "pointer",
          }}>
            {loading ? "Running..." : "Run OCR"}
          </button>

          <div style={{ flex: 1, display: "flex", gap: 12 }}>
            <button
              onClick={() => window.open(`${API}/export/${sessionId}`, "_blank")}
              style={{
                background: "#334155", color: "#f8fafc", border: "1px solid #475569", borderRadius: 6,
                padding: "6px 12px", fontSize: 13, fontWeight: 600, cursor: "pointer",
              }}
            >
              Download Markdown
            </button>
            <button onClick={download} disabled={saving || loading || !doc} style={{
              background: saving || loading || !doc ? "#334155" : "#6366f1",
              color: "#fff", border: "none", borderRadius: 6, padding: "6px 12px",
              fontSize: 13, fontWeight: 700,
              cursor: saving || loading || !doc ? "not-allowed" : "pointer",
            }}>
              {saving ? "Generating..." : "Download PDF"}
            </button>
          </div>
        </div>
      </div>

      {err && (
        <div style={{ background: "#450a0a", color: "#fca5a5", borderRadius: 6, padding: "8px 10px", fontSize: 12, marginBottom: 12 }}>
          {err}
        </div>
      )}
      {loading && (
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 14, marginTop: 60 }}>
          <Spinner />
          <div style={{ color: "#64748b", fontSize: 13 }}>Analysing layout &amp; reconstructing document...</div>
        </div>
      )}

      {doc && doc.pages.map(page => (
        <div key={page.page} style={{
          display: "flex", gap: 20, marginBottom: 32,
          alignItems: "flex-start",
        }}>
          {/* Left: original scan */}
          <div style={{ flex: "0 0 46%" }}>
            <div style={{ fontSize: 11, color: "#64748b", marginBottom: 6 }}>Original (page {page.page + 1})</div>
            {pageImg(page.page) && (
              <img src={pageImg(page.page)} alt={`page ${page.page + 1}`}
                style={{ width: "100%", border: "1px solid #1e293b", borderRadius: 6 }} />
            )}
          </div>
          {/* Right: editable structured view */}
          <div style={{
            flex: 1, background: "#f8fafc", color: "#0f172a", borderRadius: 6,
            padding: 24, minHeight: 200,
          }}>
            <div style={{ fontSize: 11, color: "#64748b", marginBottom: 10 }}>Editable content</div>
            {page.elements.map(el => {
              if (el.type === "table") {
                const rows = rowsFor(el);
                return (
                  <table key={el.id} style={{ borderCollapse: "collapse", margin: "10px 0", width: "100%" }}>
                    <tbody>
                      {rows.map((row, ri) => (
                        <tr key={ri}>
                          {row.map((cell, ci) => (
                            <td key={ci} style={cellStyle}
                              contentEditable suppressContentEditableWarning
                              onBlur={e => setCell(el.id, rows, ri, ci, e.currentTarget.textContent)}>
                              {cell}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                );
              }
              if (el.type === "image") {
                const src = el.img_path ? `${API}/images/${el.img_path.split('/').pop()}` : null;
                return src ? (
                  <div key={el.id} style={{ margin: "16px 0", textAlign: "center" }}>
                    <img src={src} alt="Extracted content" style={{ maxWidth: "100%", borderRadius: 6, border: "1px solid #e2e8f0" }} />
                  </div>
                ) : null;
              }

              const isEditing = editingBlockId === el.id;
              const isHeader = el.type === "header";
              const textVal = textFor(el);

              if (isEditing) {
                return (
                  <textarea
                    key={el.id}
                    value={textVal}
                    autoFocus
                    onChange={e => setText(el.id, e.target.value)}
                    rows={isHeader ? 1 : Math.max(2, (textVal.match(/\n/g) || []).length + 1)}
                    style={{
                      display: "block", width: "100%", border: "1px solid #6366f1",
                      borderRadius: 4, padding: "8px", margin: "8px 0",
                      fontSize: 14, color: "#0f172a", background: "#fff",
                      resize: "vertical", lineHeight: 1.5, fontFamily: "monospace",
                    }}
                    onBlur={() => setEditingBlockId(null)}
                  />
                );
              }

              return (
                <div
                  key={el.id}
                  onClick={() => setEditingBlockId(el.id)}
                  style={{
                    margin: isHeader ? "16px 0 8px" : "8px 0", padding: "4px 8px",
                    cursor: "text", border: "1px solid transparent", borderRadius: 4,
                  }}
                  onMouseEnter={e => e.currentTarget.style.background = "#f1f5f9"}
                  onMouseLeave={e => e.currentTarget.style.background = "transparent"}
                  title="Click to edit raw markdown"
                >
                  <ReactMarkdown
                    remarkPlugins={[remarkMath]}
                    rehypePlugins={[rehypeKatex]}
                  >
                    {textVal || (isHeader ? "# Empty Header" : "")}
                  </ReactMarkdown>
                </div>
              );
            })}
            {page.elements.length === 0 && (
              <div style={{ color: "#94a3b8", fontSize: 13 }}>No content detected on this page.</div>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

export default function App() {
  const [docInfo, setDocInfo] = useState(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [activeTool, setActiveTool] = useState("edit");
  const [selectedBlockIds, setSelectedBlockIds] = useState(new Set());
  const [editedBlocks, setEditedBlocks] = useState({});   // id -> partial overrides
  const [scale, setScale] = useState(1.0);
  const [documentFont, setDocumentFont] = useState("Original");
  const [history, setHistory] = useState([{}]);
  const [historyIndex, setHistoryIndex] = useState(0);
  const historyTimeoutRef = useRef(null);
  const [dragOver, setDragOver] = useState(false);
  const [editMode, setEditMode] = useState("standard");
  const [aiEditProvider, setAiEditProvider] = useState("gemini"); // 'gemini' | 'nvidia'
  const [aiEditApiKey, setAiEditApiKey] = useState("");
  const [nvidiaApiKey, setNvidiaApiKey] = useState(""); // used by Reconstruct/OCR panel (Nemotron)
  const [aiReplacementText, setAiReplacementText] = useState("");
  const [aiPadding, setAiPadding] = useState(8);
  const [aiLoading, setAiLoading] = useState(false);
  const [aiPreview, setAiPreview] = useState(null);
  const [aiError, setAiError] = useState(null);
  const [aiMessage, setAiMessage] = useState(null);
  const [scanEditMode, setScanEditMode] = useState(null);  // 'reconstruct' | 'ai_image'
  const [mineruBackend, setMineruBackend] = useState("hybrid-http-client");
  const [mineruEffort, setMineruEffort] = useState("medium");
  const fileInputRef = useRef(null);

  // Load API keys on mount
  useEffect(() => {
    const savedProvider = sessionStorage.getItem("pdfedit_ai_edit_provider");
    if (savedProvider === "gemini" || savedProvider === "replicate") {
      setAiEditProvider(savedProvider);
    }
    const initialProvider = (savedProvider === "gemini" || savedProvider === "replicate")
      ? savedProvider : "gemini";
    const savedKey = sessionStorage.getItem(`pdfedit_ai_edit_key_${initialProvider}`);
    if (savedKey) setAiEditApiKey(savedKey);

    const savedNvidia = sessionStorage.getItem("pdfedit_nvidia_api_key");
    if (savedNvidia) setNvidiaApiKey(savedNvidia);
  }, []);

  // When the provider dropdown changes, swap in that provider's saved key
  // (rather than clearing the field or leaking the other provider's key in).
  useEffect(() => {
    sessionStorage.setItem("pdfedit_ai_edit_provider", aiEditProvider);
    const savedKey = sessionStorage.getItem(`pdfedit_ai_edit_key_${aiEditProvider}`) || "";
    setAiEditApiKey(savedKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [aiEditProvider]);

  useEffect(() => {
    if (aiEditApiKey) {
      sessionStorage.setItem(`pdfedit_ai_edit_key_${aiEditProvider}`, aiEditApiKey);
    } else {
      sessionStorage.removeItem(`pdfedit_ai_edit_key_${aiEditProvider}`);
    }
  }, [aiEditApiKey, aiEditProvider]);

  useEffect(() => {
    if (nvidiaApiKey) {
      sessionStorage.setItem("pdfedit_nvidia_api_key", nvidiaApiKey);
    } else {
      sessionStorage.removeItem("pdfedit_nvidia_api_key");
    }
  }, [nvidiaApiKey]);

  // For the properties panel, show the first selected block
  const primarySelectedId = selectedBlockIds.size > 0 ? [...selectedBlockIds][0] : null;

  // Derive selected block (merged original + edits)
  const selectedBlock = (() => {
    if (!primarySelectedId || !docInfo) return null;
    for (const page of docInfo.pages) {
      const base = page.text_blocks.find(b => b.id === primarySelectedId);
      if (base) return { ...base, ...(editedBlocks[primarySelectedId] || {}) };
    }
    return null;
  })();

  const uploadFile = async (file) => {
    if (!file || !file.name.toLowerCase().endsWith(".pdf")) {
      setError("Please upload a PDF file.");
      return;
    }
    setError(null);
    setLoading(true);
    setDocInfo(null);
    setSelectedBlockIds(new Set());
    setEditedBlocks({});
    setHistory([{}]);
    setHistoryIndex(0);
    setAiPreview(null);
    setAiError(null);
    setAiMessage(null);
    setScanEditMode(null);

    try {
      const form = new FormData();
      form.append("file", file);
      const res = await fetch(`${API}/upload`, { method: "POST", body: form });
      if (!res.ok) throw new Error(`Server error: ${res.status}`);
      const data = await res.json();
      const blockCount = data.pages.reduce((n, p) => n + (p.text_blocks?.length || 0), 0);
      if (blockCount === 0) {
        setError("No editable text was detected in this PDF.");
      }
      setDocInfo(data);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const handleDrop = (e) => {
    e.preventDefault();
    setDragOver(false);
    const file = e.dataTransfer.files[0];
    uploadFile(file);
  };

  const handleFileInput = (e) => uploadFile(e.target.files[0]);

  const handleSelectBlock = (block) => {
    if (block) {
      setSelectedBlockIds(new Set([block.id]));
      setAiReplacementText(block.text || "");
      // Auto-switch to uniform font when user starts editing
      if (documentFont === "Original") {
        const fn = (block.font_name || "").toLowerCase();
        if (fn.includes("times") || fn.includes("roman") || fn.includes("garamond") || fn.includes("serif")) {
          setDocumentFont("Times Roman");
        } else if (fn.includes("courier") || fn.includes("mono") || fn.includes("consolas")) {
          setDocumentFont("Courier");
        } else {
          setDocumentFont("Helvetica");
        }
      }
    } else {
      setSelectedBlockIds(new Set());
      setAiReplacementText("");
    }
    setAiPreview(null);
    setAiError(null);
    setAiMessage(null);
  };

  const handleRectSelect = (blocks) => {
    setSelectedBlockIds(new Set(blocks.map(b => b.id)));
  };

  const handleUndo = () => {
    if (historyIndex > 0) {
      const nextIndex = historyIndex - 1;
      setHistoryIndex(nextIndex);
      setEditedBlocks(history[nextIndex]);
    }
  };

  const handleRedo = () => {
    if (historyIndex < history.length - 1) {
      const nextIndex = historyIndex + 1;
      setHistoryIndex(nextIndex);
      setEditedBlocks(history[nextIndex]);
    }
  };

  const handleUpdateBlock = (updates, isTyping = false) => {
    if (selectedBlockIds.size === 0) return;
    setEditedBlocks(prev => {
      const next = { ...prev };
      for (const id of selectedBlockIds) {
        next[id] = { ...(prev[id] || {}), ...updates };
      }
      if (isTyping) {
        if (historyTimeoutRef.current) {
          clearTimeout(historyTimeoutRef.current);
        }
        historyTimeoutRef.current = setTimeout(() => {
          setHistory(prevHist => {
            const nextHist = prevHist.slice(0, historyIndex + 1);
            nextHist.push(next);
            setHistoryIndex(nextHist.length - 1);
            return nextHist;
          });
        }, 800);
      } else {
        if (historyTimeoutRef.current) {
          clearTimeout(historyTimeoutRef.current);
        }
        setHistory(prevHist => {
          const nextHist = prevHist.slice(0, historyIndex + 1);
          nextHist.push(next);
          setHistoryIndex(nextHist.length - 1);
          return nextHist;
        });
      }
      return next;
    });
  };

  const handleAiReplace = async () => {
    if (!docInfo || !selectedBlock) return;
    setAiLoading(true);
    setAiError(null);
    setAiMessage(null);

    try {
      const res = await fetch(`${API}/ai-edit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: docInfo.session_id,
          page_number: selectedBlock.page,
          bbox: {
            x: selectedBlock.x,
            y: selectedBlock.y,
            width: selectedBlock.width,
            height: selectedBlock.height,
          },
          original_text: selectedBlock.text,
          replacement_text: aiReplacementText,
          padding: aiPadding,
          gemini_api_key: aiEditProvider === "gemini" ? aiEditApiKey : null,
          replicate_api_key: aiEditProvider === "replicate" ? aiEditApiKey : null,
        }),
      });

      let data;
      try {
        data = await res.json();
      } catch (err) {
        throw new Error(`Server error: ${res.status} - ${res.statusText}`);
      }
      if (!res.ok) {
        throw new Error(data?.detail || "AI edit preparation failed");
      }

      setAiPreview(data);
      setAiMessage(data.message || "AI edit crop prepared.");
    } catch (err) {
      setAiError(err.message);
    } finally {
      setAiLoading(false);
    }
  };

  const handleSave = async () => {
    if (!docInfo) return;
    setSaving(true);
    try {
      let blocksToSave = [];
      if (documentFont !== "Original") {
        for (const page of docInfo.pages) {
          for (const block of page.text_blocks) {
            const changes = editedBlocks[block.id] || {};
            blocksToSave.push({ block, changes });
          }
        }
      } else {
        for (const [id, changes] of Object.entries(editedBlocks)) {
          let block = null;
          for (const page of docInfo.pages) {
            block = page.text_blocks.find(b => b.id === id);
            if (block) break;
          }
          if (block) {
            blocksToSave.push({ block, changes });
          }
        }
      }

      const edits = blocksToSave.map(({ block, changes }) => {
        const merged = { ...block, ...changes };
        return {
          block_id: block.id,
          page: merged.page,
          text: merged.text,
          x: merged.x,
          y: merged.y,
          width: merged.width,
          height: merged.height,
          font_name: merged.font_name,
          font_size: merged.font_size,
          color: merged.color,
          background_color: merged.background_color,
          is_ai_edit: merged.is_ai_edit || false,
          edit_id: merged.edit_id || null,
          baseline: merged.baseline ?? null,
          pdf_font: merged.pdf_font ?? null,
          bold: merged.bold || false,
          italic: merged.italic || false,
          is_edited: !!editedBlocks[block.id],
        };
      });

      const res = await fetch(`${API}/save`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: docInfo.session_id,
          edits,
          font_family: documentFont === "Original" ? null : documentFont
        }),
      });

      if (!res.ok) throw new Error("Save failed");
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `edited_${docInfo.filename}`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  };

  const editedCount = Object.keys(editedBlocks).length;

  return (
    <div style={{
      minHeight: "100vh", background: "#020617",
      fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
      color: "#f8fafc",
      display: "flex", flexDirection: "column",
    }}>
      <ToolBar
        activeTool={activeTool}
        setActiveTool={setActiveTool}
        onSave={handleSave}
        saving={saving}
        hasDoc={!!docInfo}
        onUndo={handleUndo}
        onRedo={handleRedo}
        canUndo={historyIndex > 0}
        canRedo={historyIndex < history.length - 1}
        documentFont={documentFont}
        setDocumentFont={setDocumentFont}
      />

      {docInfo && docInfo.is_scanned && scanEditMode === null && !loading && (
        <ScanModeModal
          onReconstruct={() => setScanEditMode("reconstruct")}
          onAiImage={() => setScanEditMode("ai_image")}
          mineruBackend={mineruBackend}
          setMineruBackend={setMineruBackend}
          mineruEffort={mineruEffort}
          setMineruEffort={setMineruEffort}
          nvidiaApiKey={nvidiaApiKey}
          setNvidiaApiKey={setNvidiaApiKey}
        />
      )}

      {aiLoading && (
        <div style={{
          position: "fixed", inset: 0, background: "rgba(2,6,23,0.6)",
          display: "flex", flexDirection: "column", alignItems: "center",
          justifyContent: "center", gap: 14, zIndex: 60,
        }}>
          <Spinner />
          <div style={{ color: "#e2e8f0", fontSize: 14 }}>AI is editing the page...</div>
        </div>
      )}

      <div style={{ display: "flex", flex: 1, overflow: "hidden" }}>

        {/* ── Left sidebar: upload / status ─────────────────────────────── */}
        <div style={{
          width: 220, background: "#0f172a", borderRight: "1px solid #1e293b",
          display: "flex", flexDirection: "column", padding: 16, gap: 12,
          flexShrink: 0,
        }}>
          <button
            onClick={() => fileInputRef.current?.click()}
            style={{
              background: "#6366f1", color: "#fff", border: "none",
              borderRadius: 8, padding: "10px 0", fontWeight: 700,
              fontSize: 13, cursor: "pointer", width: "100%",
            }}
          >
            + Open PDF
          </button>
          <input
            ref={fileInputRef} type="file" accept=".pdf"
            style={{ display: "none" }} onChange={handleFileInput}
          />

          {docInfo && (
            <>
              <div style={{ fontSize: 11, color: "#64748b", borderTop: "1px solid #1e293b", paddingTop: 12 }}>
                <div style={{ color: "#f8fafc", fontWeight: 600, fontSize: 12, marginBottom: 6, wordBreak: "break-word" }}>
                  {docInfo.filename}
                </div>
                <div>{docInfo.num_pages} page{docInfo.num_pages !== 1 ? "s" : ""}</div>
                <div style={{ marginTop: 4 }}>
                  {docInfo.pages.reduce((n, p) => n + p.text_blocks.length, 0)} editable text block
                  {docInfo.pages.reduce((n, p) => n + p.text_blocks.length, 0) !== 1 ? "s" : ""}
                </div>
                {docInfo.is_scanned && <div style={{ color: "#f59e0b", marginTop: 4 }}>Contains scanned pages</div>}
                {docInfo.is_scanned && scanEditMode && (
                  <div style={{ marginTop: 6 }}>
                    <span style={{ color: "#94a3b8" }}>
                      Mode: {scanEditMode === "reconstruct" ? "Reconstruct & Edit" : "AI Image Edit"}
                    </span>
                    <button onClick={() => setScanEditMode(null)} style={{
                      marginLeft: 8, background: "none", border: "none",
                      color: "#6366f1", cursor: "pointer", fontSize: 11, padding: 0,
                      textDecoration: "underline",
                    }}>change</button>
                  </div>
                )}
                {editedCount > 0 && (
                  <div style={{ color: "#10b981", marginTop: 4 }}>
                    {editedCount} block{editedCount !== 1 ? "s" : ""} edited
                  </div>
                )}
                {selectedBlockIds.size > 1 && (
                  <div style={{ color: "#6366f1", marginTop: 4 }}>
                    {selectedBlockIds.size} blocks selected
                  </div>
                )}
              </div>

              {/* Scale control */}
              <div style={{ borderTop: "1px solid #1e293b", paddingTop: 12 }}>
                <div style={{ fontSize: 11, color: "#64748b", marginBottom: 6 }}>Zoom: {Math.round(scale * 100)}%</div>
                <input
                  type="range" min={0.4} max={2.0} step={0.05}
                  value={scale}
                  onChange={e => setScale(parseFloat(e.target.value))}
                  style={{ width: "100%" }}
                />
              </div>
            </>
          )}

          {error && (
            <div style={{
              background: "#450a0a", color: "#fca5a5", borderRadius: 6,
              padding: "8px 10px", fontSize: 12,
            }}>
              {error}
            </div>
          )}
        </div>

        {/* ── Centre: PDF canvas ─────────────────────────────────────────── */}
        <div
          style={{
            flex: 1, overflow: "auto", padding: "32px 40px 60px",
            display: "flex", flexDirection: "column", alignItems: "center",
            background: dragOver ? "#0f172a" : "#020617",
            transition: "background 0.15s",
          }}
          onDragOver={e => { e.preventDefault(); setDragOver(true); }}
          onDragLeave={() => setDragOver(false)}
          onDrop={handleDrop}
        >
          {loading && (
            <div style={{ marginTop: 80, display: "flex", flexDirection: "column", alignItems: "center", gap: 16 }}>
              <Spinner />
              <div style={{ color: "#64748b", fontSize: 14 }}>Processing PDF...</div>
              <div style={{ color: "#475569", fontSize: 12 }}>
                Scanned pages will be OCR'd automatically
              </div>
            </div>
          )}

          {!loading && !docInfo && (
            <div
              style={{
                marginTop: 80, width: "100%", maxWidth: 480,
                border: `2px dashed ${dragOver ? "#6366f1" : "#1e293b"}`,
                borderRadius: 16, padding: "60px 40px",
                display: "flex", flexDirection: "column", alignItems: "center", gap: 16,
                cursor: "pointer", transition: "border-color 0.15s",
              }}
              onClick={() => fileInputRef.current?.click()}
            >
              <div style={{ fontSize: 48 }}>PDF</div>
              <div style={{ fontSize: 16, fontWeight: 700, color: "#f8fafc" }}>
                Drop your PDF here
              </div>
              <div style={{ fontSize: 13, color: "#64748b", textAlign: "center" }}>
                Works with both digital PDFs and scanned image PDFs.
                Scanned pages are automatically OCR'd.
              </div>
              <button style={{
                marginTop: 8, background: "#6366f1", color: "#fff",
                border: "none", borderRadius: 8, padding: "10px 24px",
                fontSize: 14, fontWeight: 600, cursor: "pointer",
              }}>
                Choose File
              </button>
            </div>
          )}

          {docInfo && scanEditMode === "reconstruct" ? (
            <ReconstructEditor
              sessionId={docInfo.session_id}
              pages={docInfo.pages}
              filename={docInfo.filename}
              onBack={() => setScanEditMode(null)}
              nvidiaApiKey={nvidiaApiKey}
              setNvidiaApiKey={setNvidiaApiKey}
              mineruBackend={mineruBackend}
              mineruEffort={mineruEffort}
            />
          ) : (
            docInfo && docInfo.pages.map(page => (
              <PDFPage
                key={page.page_number}
                pageInfo={page}
                scale={scale}
                activeTool={activeTool}
                selectedIds={selectedBlockIds}
                onSelectBlock={handleSelectBlock}
                onRectSelect={handleRectSelect}
                editedBlocks={editedBlocks}
                selectedBlock={selectedBlock}
                onUpdateBlock={handleUpdateBlock}
                documentFont={documentFont}
              />
            ))
          )}
        </div>

        {/* ── Right sidebar: properties ──────────────────────────────────── */}
        <div style={{
          width: 240, background: "#0f172a", borderLeft: "1px solid #1e293b",
          flexShrink: 0, overflowY: "auto",
        }}>
          <div style={{
            padding: "12px 16px", borderBottom: "1px solid #1e293b",
            fontSize: 12, fontWeight: 700, color: "#64748b",
            textTransform: "uppercase", letterSpacing: 1,
          }}>
            Properties
          </div>
          <PropertiesPanel
            selectedBlock={selectedBlock}
            onUpdate={handleUpdateBlock}
            editMode={editMode}
            setEditMode={setEditMode}
            aiReplacementText={aiReplacementText}
            setAiReplacementText={setAiReplacementText}
            aiPadding={aiPadding}
            setAiPadding={setAiPadding}
            aiEditProvider={aiEditProvider}
            setAiEditProvider={setAiEditProvider}
            aiEditApiKey={aiEditApiKey}
            setAiEditApiKey={setAiEditApiKey}
            onAiReplace={handleAiReplace}
            aiLoading={aiLoading}
            aiPreview={aiPreview}
            aiError={aiError}
            aiMessage={aiMessage}
          />
        </div>
      </div>
    </div>
  );
}
