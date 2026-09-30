# PlotSystem · Spectrum Studio

Selected UI/UX Pro Max preset 88, Adobe Spectrum (`spectrum-design-system`), for a professional writing and simulation workspace. Independent baseline: af4ada4.

## Design decisions

- Neutral application framing, distinct panel layers, restrained blue interaction states and a small warm brand accent.
- Persistent navigation, searchable project library, project overview, paired goal/material panels, full-width knowledge graph and character collection.
- System UI fonts for controls; preserve self-hosted Noto Serif SC for narrative reading.
- Existing Vue components use an adaptation of Spectrum 1 semantic grays and accent colors; this is not an official Adobe implementation or a migration to React Spectrum.
- Maintain existing APIs and data. Reuse verified project loading feedback, creation focus management and graph auto-resizing from prior experiments.
- Independent theme preference, light and dark palettes, keyboard skip link and focus indicators, responsive navigation and reduced motion.

## Sources

- [Spectrum color system](https://spectrum.adobe.com/page/color-system/): gray text hierarchy and semantic accent usage.
- [Using color](https://spectrum.adobe.com/page/using-color/): application background layers and interaction states.
- [Color fundamentals](https://spectrum.adobe.com/page/color-fundamentals/): Spectrum 1 blue-900 light and dark values.

The skill's broad design-system search suggested a generic marketing layout. The explicit Adobe Spectrum style match and the existing creative tool workflow guide this implementation instead.
