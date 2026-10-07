# Slide source subset

The source is a limited JSX-like declarative language, not React, a browser or arbitrary JavaScript. Use supported literal props/styles and explicit components; `.map()` expansion is unsupported. Each `.slide` has one root `Slide` with properly closed elements. UTF-8 source supports Chinese and English. The canvas is 1280 × 720 px; rendered objects use native PPTX coordinates after conversion.

```jsx
<Slide notes="Source and speaker notes" style={{padding:48,background:'#FFFFFF'}}>
  <Text style={{fontSize:40,fontWeight:'bold',color:'#17324D'}}>Title</Text>
  <Box style={{flexDirection:'row',gap:24}}>
    <Text style={{fontSize:24,width:520}}>Evidence and explanation<br />A second line</Text>
    <Image src="assets/figure.png" style={{width:480,height:300}} />
  </Box>
</Slide>
```

## Components

- `Slide`: canvas, padding, background and speaker `notes`.
- `Box`: flex row/column, gap, dimensions, alignment and optional absolute position. Off-canvas geometry is rejected, including absolute elements.
- `Text`, nested `span` and `br`: native text, font size/family/weight, color, paragraph alignment and hyperlinks. Prefer explicit font choices that are present in the final viewer.
- `Image`: verified local project-relative raster asset. URLs and paths outside the project are rejected. Pictures remain embedded in PPTX.
- `Table`: `cells={[["A","B"],["1","2"]]}`; native editable table.
- `Chart`: e.g. `chartType="barChart" categories={["Q1","Q2"]} series={[{name:'Revenue',values:[2,3]}]}`; native editable chart. Supported category types include line, bar/column, pie/doughnut and area; use documented types, not guessed names.
- `FAIcon`: local native-shape/glyph mapping with explicit `name` and `fill`; only the implemented mapping is available.
- `QRCode`: explicit `text`; a generated raster code, not an editable vector.
- `CodeBlock`: a native text presentation of code.
- `SVG`: a small rasterized primitive/path subset; it is not a full SVG renderer. Complex SVG should be explicitly converted to a verified local image before building.

Box/text styles include `width`, `height`, `padding`, `margin`, `gap`, `flexDirection`, `flex`, `justifyContent`, `alignItems`, `position`, `left/top/right/bottom`, `fontSize`, `fontFamily`, `fontWeight`, `lineHeight`, `textAlign`, `color`, `background`, border and opacity. Unsupported grid/calc and unknown components are errors. This subset does not implement diagrams through a remote service or preserve animation.

Font fitting is estimated from character widths; lint success is not proof of final PowerPoint font fitting. Density and missing-padding diagnostics are design hints, while missing required resources, unsupported syntax and geometry violations block output.
