import { useState, useRef, useCallback } from "react";

const API = "http://127.0.0.1:8000";

// The backend renders page images at 150 DPI but returns text coordinates
// in PDF points (72 DPI). This ratio converts points → image pixels.
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

function ToolBar({ activeTool, setActiveTool, onSave, saving, hasDoc }) {
  const tools = [
    { id: "select", icon: "⬚", label: "Select Region" },
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
      <div style={{ flex: 1 }} />
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
          {saving ? "Saving…" : "⬇ Download Edited PDF"}
        </button>
      )}
    </div>
  );
}

function PropertiesPanel({ selectedBlock, onUpdate }) {
  if (!selectedBlock) {
    return (
      <div style={{ padding: 20, color: "#64748b", fontSize: 13, textAlign: "center" }}>
        <div style={{ fontSize: 28, marginBottom: 8 }}>T</div>
        Click any text block to edit it,
        <br />or use <b>Select Region</b> to drag a rectangle
      </div>
    );
  }

  const { text, font_name, font_size, color } = selectedBlock;

  return (
    <div style={{ padding: 16, display: "flex", flexDirection: "column", gap: 14 }}>
      <div style={{ color: "#94a3b8", fontSize: 11, fontWeight: 700, letterSpacing: 1, textTransform: "uppercase" }}>
        Text Properties
      </div>

      <label style={labelStyle}>
        <span>Content</span>
        <textarea
          value={text}
          onChange={e => onUpdate({ text: e.target.value })}
          style={{ ...inputStyle, minHeight: 80, resize: "vertical", lineHeight: 1.5 }}
        />
      </label>

      <label style={labelStyle}>
        <span>Font Family</span>
        <select
          value={font_name}
          onChange={e => onUpdate({ font_name: e.target.value })}
          style={inputStyle}
        >
          {FONTS.map(f => <option key={f} value={f}>{f}</option>)}
        </select>
      </label>

      <label style={labelStyle}>
        <span>Font Size</span>
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <input
            type="range" min={6} max={72} step={0.5}
            value={font_size}
            onChange={e => onUpdate({ font_size: parseFloat(e.target.value) })}
            style={{ flex: 1 }}
          />
          <span style={{ color: "#f8fafc", width: 30, fontSize: 12 }}>{font_size}</span>
        </div>
      </label>

      <label style={labelStyle}>
        <span>Color</span>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <input
            type="color"
            value={color}
            onChange={e => onUpdate({ color: e.target.value })}
            style={{ width: 40, height: 32, border: "none", borderRadius: 4, cursor: "pointer", background: "none" }}
          />
          <span style={{ color: "#94a3b8", fontSize: 12 }}>{color}</span>
        </div>
      </label>

      {selectedBlock.is_ocr && (
        <div style={{
          background: "#1e293b", borderRadius: 6, padding: "8px 10px",
          fontSize: 11, color: "#f59e0b", display: "flex", gap: 6,
        }}>
          ⚡ OCR-detected text — check for accuracy
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

function PDFPage({ pageInfo, scale, activeTool, selectedIds, onSelectBlock, onRectSelect, editedBlocks }) {
  const canvasRef = useRef(null);
  const [hoverId, setHoverId] = useState(null);
  const [dragStart, setDragStart] = useState(null);  // { x, y } in pixels relative to container
  const [dragEnd, setDragEnd] = useState(null);

  // Merge original blocks with any local edits
  const blocks = pageInfo.text_blocks.map(b => ({
    ...b,
    ...(editedBlocks[b.id] || {}),
  }));

  // Container is sized to the actual image pixel dimensions (points × DPI_RATIO),
  // then scaled by the user-zoom factor.
  const pw = pageInfo.width * DPI_RATIO * scale;
  const ph = pageInfo.height * DPI_RATIO * scale;

  // Combined scale factor: points → display pixels
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

          return (
            <div
              key={block.id}
              style={{
                position: "absolute",
                left: block.x * totalScale,
                top: block.y * totalScale,
                width: Math.max(block.width + 4, 8) * totalScale,
                minWidth: 24,
                height: Math.max(block.height, 10) * totalScale,
                minHeight: 16,
                // Only make text visible if it has been edited
                color: editedBlocks[block.id] ? block.color : "transparent",
                fontFamily: FONT_CSS_MAP[block.font_name] || "Helvetica",
                fontSize: block.font_size * totalScale,
                whiteSpace: "pre-wrap",
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
                // If it's edited, give it a solid white background to cover the original image text
                background: editedBlocks[block.id]
                  ? "#ffffff"
                  : isSelected
                    ? "rgba(99,102,241,0.3)"
                    : isHovered
                      ? "rgba(148,163,184,0.2)"
                      : "transparent",
                boxSizing: "border-box",
                transition: "outline 0.1s, background 0.1s",
                padding: "0 2px", // padding so text doesn't hit the very edge
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
              {editedBlocks[block.id] ? block.text : null}
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

      {/* Page number badge */}
      <div style={{
        position: "absolute", bottom: -20, left: 0, right: 0,
        textAlign: "center", fontSize: 11, color: "#64748b",
      }}>
        Page {pageInfo.page_number + 1}
        {pageInfo.is_scanned && <span style={{ color: "#f59e0b", marginLeft: 6 }}>● OCR</span>}
      </div>
    </div>
  );
}

// ── Main App ──────────────────────────────────────────────────────────────────

export default function App() {
  const [docInfo, setDocInfo] = useState(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [activeTool, setActiveTool] = useState("edit");
  const [selectedBlockIds, setSelectedBlockIds] = useState(new Set());
  const [editedBlocks, setEditedBlocks] = useState({});   // id -> partial overrides
  const [scale, setScale] = useState(1.0);
  const [dragOver, setDragOver] = useState(false);
  const fileInputRef = useRef(null);

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
    } else {
      setSelectedBlockIds(new Set());
    }
  };

  const handleRectSelect = (blocks) => {
    setSelectedBlockIds(new Set(blocks.map(b => b.id)));
  };

  const handleUpdateBlock = (updates) => {
    if (selectedBlockIds.size === 0) return;
    // Apply updates to all selected blocks
    setEditedBlocks(prev => {
      const next = { ...prev };
      for (const id of selectedBlockIds) {
        next[id] = { ...(prev[id] || {}), ...updates };
      }
      return next;
    });
  };

  const handleSave = async () => {
    if (!docInfo) return;
    setSaving(true);
    try {
      // Build edit list: only blocks that were actually changed
      const edits = Object.entries(editedBlocks).map(([id, changes]) => {
        // Find original block
        let original = null;
        for (const page of docInfo.pages) {
          original = page.text_blocks.find(b => b.id === id);
          if (original) break;
        }
        if (!original) return null;
        const merged = { ...original, ...changes };
        return {
          block_id: id,
          page: merged.page,
          text: merged.text,
          x: merged.x,
          y: merged.y,
          width: merged.width,
          height: merged.height,
          font_name: merged.font_name,
          font_size: merged.font_size,
          color: merged.color,
        };
      }).filter(Boolean);

      const res = await fetch(`${API}/save`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: docInfo.session_id, edits }),
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
      />

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
                {docInfo.is_scanned && <div style={{ color: "#f59e0b", marginTop: 4 }}>⚡ Contains scanned pages</div>}
                {editedCount > 0 && (
                  <div style={{ color: "#10b981", marginTop: 4 }}>
                    ✎ {editedCount} block{editedCount !== 1 ? "s" : ""} edited
                  </div>
                )}
                {selectedBlockIds.size > 1 && (
                  <div style={{ color: "#6366f1", marginTop: 4 }}>
                    ⬚ {selectedBlockIds.size} blocks selected
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
              <div style={{ color: "#64748b", fontSize: 14 }}>Processing PDF…</div>
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
              <div style={{ fontSize: 48 }}>📄</div>
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

          {docInfo && docInfo.pages.map(page => (
            <PDFPage
              key={page.page_number}
              pageInfo={page}
              scale={scale}
              activeTool={activeTool}
              selectedIds={selectedBlockIds}
              onSelectBlock={handleSelectBlock}
              onRectSelect={handleRectSelect}
              editedBlocks={editedBlocks}
            />
          ))}
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
          />
        </div>
      </div>
    </div>
  );
}
