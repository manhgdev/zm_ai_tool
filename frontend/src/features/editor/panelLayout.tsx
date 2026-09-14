/** Panel layout của editor: kích thước mặc định, panel kéo-thả (dnd-kit)
 *  và persistence công cụ timeline — tách khỏi LivePreviewEditor.tsx. */
import React from 'react'
import { Panel } from 'react-resizable-panels'
import { useSortable } from '@dnd-kit/sortable'
import { CSS as DndCSS } from '@dnd-kit/utilities'
import { cn } from '@/shared/lib/cn'
import { captionFontCss } from '@/features/editor/lib'

export const TIMELINE_TOOLS_STORAGE_KEY = 'zm_ai_tool.editor.timeline-tools'

export function loadTimelineTool(name: 'mainTrackMagnet' | 'autoSnapping' | 'mediaLinked') {
  try {
    const saved = JSON.parse(localStorage.getItem(TIMELINE_TOOLS_STORAGE_KEY) || '{}') as Record<string, unknown>
    return typeof saved[name] === 'boolean' ? saved[name] : true
  } catch {
    return true
  }
}

export async function loadCaptionFont(family: string, text = 'Phụ đề tiếng Việt') {
  if (typeof document === 'undefined' || !document.fonts) return
  try {
    await document.fonts.load(`700 48px ${captionFontCss(family)}`, text)
  } catch {
    // The bundled fallback remains usable; relayout still runs below.
  }
}

export type PanelId = 'tools' | 'preview' | 'properties'

export const PANEL_SIZES: Record<PanelId, { defaultSize: number; minSize?: number; maxSize?: number; className: string }> = {
  tools:      { defaultSize: 25, minSize: 12, maxSize: 45, className: 'min-w-0' },
  preview:    { defaultSize: 50, minSize: 25,               className: 'min-h-0 min-w-0' },
  properties: { defaultSize: 25, minSize: 15, maxSize: 45, className: 'min-w-0' },
}

export function SortablePanel({
  id, children, defaultSize, minSize, maxSize, className,
}: { id: PanelId; children: React.ReactNode; defaultSize: number; minSize?: number; maxSize?: number; className?: string }) {
  const { setNodeRef, attributes, listeners, transform, transition, isDragging } = useSortable({ id })
  const asPct = (v?: number) => v == null ? undefined : String(v)
  return (
    <Panel
      id={id}
      defaultSize={asPct(defaultSize)}
      minSize={asPct(minSize)}
      maxSize={asPct(maxSize)}
      className={className}
    >
      {/* wrapper captures sortable ref + shows grip */}
      <div
        ref={setNodeRef}
        className="relative h-full w-full"
        style={{
          transform: DndCSS.Transform.toString(transform),
          transition: isDragging ? undefined : transition,
          zIndex: isDragging ? 50 : undefined,
        }}
      >
        {/* Grip handles — 4 góc, hiện khi hover trực tiếp vào icon */}
        {(['tl','tr','bl','br'] as const).map((corner) => (
          <div
            key={corner}
            {...attributes}
            {...listeners}
            title="Kéo để đổi vị trí panel"
            className={cn(
              'absolute z-50 w-5 h-5 rounded-sm',
              'flex items-center justify-center',
              'cursor-grab active:cursor-grabbing select-none',
              'opacity-0 hover:opacity-100 transition-opacity duration-100',
              'bg-background/90 border border-border shadow-sm',
              corner === 'tl' && 'top-1 left-1',
              corner === 'tr' && 'top-1 right-1',
              corner === 'bl' && 'bottom-1 left-1',
              corner === 'br' && 'bottom-1 right-1',
            )}
          >
            <svg width="8" height="8" viewBox="0 0 8 8" fill="currentColor" className="text-muted-foreground">
              <rect x="0" y="0" width="3" height="3" rx="0.5"/>
              <rect x="5" y="0" width="3" height="3" rx="0.5"/>
              <rect x="0" y="5" width="3" height="3" rx="0.5"/>
              <rect x="5" y="5" width="3" height="3" rx="0.5"/>
            </svg>
          </div>
        ))}
        {children}
      </div>
    </Panel>
  )
}
