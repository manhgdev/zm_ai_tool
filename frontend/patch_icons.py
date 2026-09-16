import sys

path = 'd:/DEV/zm_ai_tool/frontend/src/shared/components/Icons.tsx'
with open(path + '.bk1', 'r', encoding='utf-8') as f:
    content = f.read()

icon_wand = '''
export function IconWand(p: Props) {
  return (
    <Svg {...p}>
      <path d="m21.64 3.64-1.28-1.28a1.21 1.21 0 0 0-1.72 0L2.36 18.64a1.21 1.21 0 0 0 0 1.72l1.28 1.28a1.21 1.21 0 0 0 1.72 0L21.64 5.36a1.21 1.21 0 0 0 0-1.72z" />
      <path d="m14 7 3 3" />
      <path d="M5 6v4" />
      <path d="M19 14v4" />
      <path d="M10 2v2" />
      <path d="M7 8H3" />
      <path d="M21 16h-4" />
      <path d="M11 3H9" />
    </Svg>
  )
}
'''
with open(path, 'w', encoding='utf-8') as f:
    f.write(content + '\n' + icon_wand)
