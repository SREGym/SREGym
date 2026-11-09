# Who uses SREGym?

The README lists organizations whose researchers or engineers have publicly documented using SREGym. A listing describes benchmark use, not a partnership or endorsement. Sources were checked on November 9, 2026.

| Organization | Evidence of use |
| --- | --- |
| Microsoft Research | The [University of Illinois announcement](https://siebelschool.illinois.edu/news/sregym) explicitly names Microsoft Research among research groups using SREGym and says Microsoft Research selected it for the System Intelligence initiative. |
| University of Washington | The [University of Illinois announcement](https://siebelschool.illinois.edu/news/sregym) explicitly names research groups at the University of Washington as users. |
| University of Illinois Urbana-Champaign | The [University of Illinois announcement](https://siebelschool.illinois.edu/news/sregym) describes the project and its use by Illinois research groups. |
| University of Toronto | The [University of Illinois announcement](https://siebelschool.illinois.edu/news/sregym) explicitly names Toronto research groups as users. |
| Chiron | The [official SREGym leaderboard](https://www.sregym.com/leaderboard) lists Chiron + Claude Code as a third-party submission and links it to chironvision.ai. |
| Datadog | Datadog's [Scout 1.0 announcement](https://www.datadoghq.com/blog/ai/introducing-scout-sre-specialized-model-bits-ai/) says it evaluated Scout on SREGym. Its [technical report, section 2.1](https://web-assets.dd-static.net/42588/1792071077-datadog-scout_technical_report.pdf) describes running the full benchmark. |
| Cognition | The company's [Devin Oncall announcement](https://devin.ai/blog/introducing-devin-oncall) describes its SREGym evaluation and SREGym-Lite results. |
| CloudThinker | CloudThinker's [SREGym results article](https://cloudthinker.io/blogs/your-on-call-has-not-met-astra-yet) describes both its submitted results and an internal evaluation campaign. |
| NOFire | NOFire's [benchmark article](https://www.nofire.ai/blog/sregym-benchmark-nofire-results) describes its own diagnosis evaluation using SREGym-Lite. |
| Skyhook | The [Radar team's SREGym article](https://radarhq.io/blog/sregym-leaderboard), written by Skyhook's CEO, documents its evaluation and upstream client. The logo names Radar underneath Skyhook and links to Radar's website. |
| Ciroos | Niall Murphy [requested adding Ciroos Inc](https://github.com/SREGym/SREGym/pull/1173#issuecomment-6746459317) in the README pull request, and the author acknowledged the request. |

The Rootconf talk linked in the reference describes someone evaluating OpenSRE, HolmesGPT, and K8sGPT using SREGym. It does not establish that the organizations maintaining those products themselves use SREGym, so those organizations are not included. Funding, a model appearing in a benchmark, a paper citation, and a company appearing in a simulated outage also do not establish use by that company.

## Logo sources

Original marks are saved in `logos/`, with download URLs in `logo-sources.json`. Sources are the organizations' own sites or official brand resources. The NOFire monochrome logo is inverted for dark mode. The Cognition mark's `currentColor` is resolved to match each theme. Microsoft's wordmark uses white lettering in dark mode while keeping its four brand colors. The Toronto wordmark uses navy in light mode, and CloudThinker's lettering uses dark text in light mode while keeping its white star. Chiron, Cognition, and Skyhook have separate text labels beside their original marks.

Every organization has a separate clickable image with a transparent background. The README uses [GitHub's theme-specific image fragments](https://github.blog/changelog/2021-11-24-specify-theme-context-for-images-in-markdown/) so the visible version follows the selected GitHub theme.

## Rebuild

Run `python3 assets/who-uses/build.py` from the repository root. Python 3 and `rsvg-convert` are required. The build uses local files and creates individual transparent PNGs at three times their base dimensions in `light/` and `dark/`.
