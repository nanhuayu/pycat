# Channel brand artwork

These files identify the third-party messaging services that PyCat connects to.
They preserve the original, full-color artwork downloaded from the services' official
websites, not generic replacement symbols or generated approximations. Preserve
their aspect ratios and original colors; do not apply the monochrome UI-icon tint.

## Rights and attribution

The names, logos, and trademarks belong to their respective owners. Their
inclusion does not indicate sponsorship, endorsement, or official affiliation.
The project's AGPL-3.0 source-code license does **not** grant rights to these
third-party trademarks or artwork. Public availability on an official website is
not itself an open-source redistribution license.

Tencent, Feishu, and DingTalk do not provide an explicit open redistribution
license on the specific source pages below. Their artwork rights remain with
their owners; this provenance record does not resolve the permission scope for
every distribution or use. Review applicable brand terms and obtain permission
where necessary before redistributing outside identification of these services.

Telegram's linked logo page expressly permits use for illustrations, graphs,
forward-to-Telegram buttons, and similar uses, provided the presentation does not
claim to represent Telegram officially. Its separate CC0 statement is about
**screenshots**, and is not asserted here as a license for the logo.

## Original sources

Retrieved and checked September 30–October 1, 2026 (UTC). All five assets are
copies of the original files with renamed local filenames. QQ and WeChat have
only trailing whitespace removed and a final newline added to satisfy the source
whitespace gate; SVG geometry, colors, and all other markup remain unchanged.
The other three files are byte-for-byte unchanged. Telegram's SVG was extracted
from its official ZIP download. There are
no runtime network requests for these assets.

### WeChat / Weixin — `wechat.svg`

- Owner: Tencent
- Official product page: <https://www.tencent.com/products/weixin-wechat/>
- Image linked by that page: <https://www.tencent.com/wp-content/uploads/2022/12/weixin-wechat-1.svg>
- Format: SVG; 50 × 50 viewBox; transparent background, original green and gray gradients
- Official download SHA-256: `4534ae3d3162aaa5bbcec507a4d76c5e5d5e8cefd686f50d463552f7f4861ea2`
- Packaged SHA-256 (whitespace normalization only): `ee10b305540ea4c022d0c94ae9c8b3f635c129b509a7dcb1f1902617610ed0f5`

### QQ — `qq.svg`

- Owner: Tencent
- Official product page: <https://www.tencent.com/products/qq/>
- Image linked by that page: <https://www.tencent.com/wp-content/uploads/2022/12/qq-1-1.svg>
- Official brand guidance: <https://qq.design/brand/BrandDesign/Logo/>
- Format: SVG; 50 × 50 viewBox; transparent background, full-color penguin
- Official download SHA-256: `111bf7db52f2ce64d9258ff521b6d7b05035df6b805df4443823e9cb7e09a8a3`
- Packaged SHA-256 (whitespace normalization only): `1c1f40543e00a66268577898d84a54cc62e70d27bf76d0ac641f931296c7ef4e`

The product-icon asset is used unchanged. The linked brand guidance calls for
using master artwork without redrawing or altering its proportions and colors.

### Feishu — `feishu.svg`

- Owner: Feishu / ByteDance
- Official developer platform: <https://open.feishu.cn/>
- SVG favicon linked by that page: <https://lf-package-cn.feishucdn.com/obj/feishu-static/lark/open/website/favicon-logo.svg>
- Format: SVG; 16 × 16 viewBox; original multicolor bird on a rounded white app tile, with transparent corners
- SHA-256: `247c4e35716c3263d04b697ad19ff1868cb8ed321a12454e27224d34ee11f1eb`

### DingTalk — `dingtalk.png`

- Owner: DingTalk
- Official homepage: <https://www.dingtalk.com/>
- PNG favicon linked by that page: <https://gw.alicdn.com/imgextra/i3/O1CN014ZbI6ZTEhdC0ttN2_!!6000000003783-2-tps-444-444.png>
- Format: PNG; 444 × 444 RGBA; blue circular app logo, transparent corners
- SHA-256: `b3fb0715857df5d23213b5265166fabcb7f7b4b290890d7b5682a027cf39c85d`

### Telegram — `telegram.svg`

- Owner: Telegram
- Official logo download and usage guidance: <https://telegram.org/tour/screenshots>
- Official archive: <https://telegram.org/file/464001088/1/bI7AJLo7oX4.287931.zip/374fe3b0a59dc60005>
- Archive member: `Logo.svg`
- Format: SVG; 1000 × 1000 viewBox; original blue gradient circle and white paper plane, transparent corners
- SHA-256: `54842d414c100f0110f90caf09ff58cd0d229629820e4576bd70befca88befd5`

## Verification

The SVGs parse as XML, are self-contained, and contain no scripts or external
resources. Qt's SVG renderer loads all four; Qt's image loader loads the PNG.
Each asset has a square canvas and transparent pixels. Original artwork was
visually inspected after rendering, including small and high-DPI icon sizes.
