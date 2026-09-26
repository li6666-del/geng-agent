import { useId } from "react";

type SignalArtworkProps = {
  compact?: boolean;
};

/** A decorative illustration of a radio signal and its constellation. */
export function SignalArtwork({ compact = false }: SignalArtworkProps) {
  const id = useId();
  const paint = (name: string) => `url(#${id}-${name})`;
  const symbols = [-36, -12, 12, 36];

  return (
    <svg
      className={`signal-artwork${compact ? " signal-artwork--compact" : ""}`}
      viewBox={compact ? "42 24 544 220" : "0 0 620 260"}
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
      focusable="false"
      style={{ display: "block", width: "100%", height: "auto", pointerEvents: "none" }}
    >
      <defs>
        <radialGradient id={`${id}-atmosphere`}>
          <stop stopColor="#DFEAF8" stopOpacity=".8" />
          <stop offset="1" stopColor="#EAF0F7" stopOpacity="0" />
        </radialGradient>
        <linearGradient id={`${id}-signal`} x1="172" y1="120" x2="406" y2="120" gradientUnits="userSpaceOnUse">
          <stop stopColor="#2B5ED7" />
          <stop offset=".55" stopColor="#3876CD" />
          <stop offset="1" stopColor="#168A9B" />
        </linearGradient>
        <linearGradient id={`${id}-disc`} x1="431" y1="58" x2="546" y2="184" gradientUnits="userSpaceOnUse">
          <stop stopColor="#FFFFFF" stopOpacity=".96" />
          <stop offset="1" stopColor="#EAF2F5" stopOpacity=".92" />
        </linearGradient>
        <linearGradient id={`${id}-mast`} x1="105" y1="135" x2="125" y2="198" gradientUnits="userSpaceOnUse">
          <stop stopColor="#7096E4" />
          <stop offset="1" stopColor="#2B5ED7" />
        </linearGradient>
        <linearGradient id={`${id}-horizon`} x1="43" y1="202" x2="576" y2="202" gradientUnits="userSpaceOnUse">
          <stop stopColor="#AAC0D4" stopOpacity="0" />
          <stop offset=".22" stopColor="#AAC0D4" stopOpacity=".65" />
          <stop offset=".78" stopColor="#AAC0D4" stopOpacity=".65" />
          <stop offset="1" stopColor="#AAC0D4" stopOpacity="0" />
        </linearGradient>
        <pattern id={`${id}-grid`} width="24" height="24" patternUnits="userSpaceOnUse">
          <path d="M24 0H0V24" stroke="#90ACC9" strokeOpacity=".12" strokeWidth=".7" />
        </pattern>
      </defs>

      <ellipse cx="330" cy="126" rx="287" ry="130" fill={paint("atmosphere")} />
      <path d="M58 166C133 45 333 15 513 68C560 82 584 103 580 125" stroke="#C6D5E5" strokeOpacity=".5" strokeWidth=".8" />
      <path d="M91 190C222 228 420 214 544 149" stroke="#C6D5E5" strokeOpacity=".55" strokeWidth=".8" />
      <ellipse cx="309" cy="154" rx="228" ry="73" fill={paint("grid")} />
      <path d="M43 202H576" stroke={paint("horizon")} />

      {/* The transmitter stands on a shallow, softly outlined plinth. */}
      <ellipse cx="114" cy="204" rx="49" ry="10" fill="#DCE6F1" fillOpacity=".48" />
      <ellipse cx="114" cy="199" rx="43" ry="9" fill="#F8FAFC" stroke="#CBD8E6" />
      <path d="M109 137L99 193H129L119 137" fill={paint("mast")} fillOpacity=".14" stroke="#2B5ED7" strokeWidth="1.5" strokeLinejoin="round" />
      <path d="M107 151L123 170L102 185M121 151L105 170L127 185M104 169H124M101 186H127" stroke="#5C82C9" strokeWidth="1.05" strokeLinecap="round" />
      <path d="M114 112V137" stroke="#2B5ED7" strokeWidth="2" strokeLinecap="round" />
      <path d="M105 137H123M95 193H133" stroke="#2B5ED7" strokeWidth="2" strokeLinecap="round" />
      <circle cx="114" cy="108" r="5" fill="#2B5ED7" />
      <circle cx="114" cy="108" r="10" stroke="#2B5ED7" strokeOpacity=".14" strokeWidth="4" />

      <g stroke="#2B5ED7" strokeLinecap="round">
        <path d="M99 92A22 22 0 0 0 99 124M129 92A22 22 0 0 1 129 124" strokeWidth="1.65" strokeOpacity=".8" />
        <path d="M88 81A37 37 0 0 0 88 135M140 81A37 37 0 0 1 140 135" strokeWidth="1.3" strokeOpacity=".45" />
        <path d="M78 71A51 51 0 0 0 78 145M150 71A51 51 0 0 1 150 145" strokeWidth="1" strokeOpacity=".22" />
      </g>

      {/* Continuous carrier waves connect to an idealised I/Q symbol space. */}
      <path d="M166 122H414" stroke="#8CA9BF" strokeWidth=".8" strokeDasharray="3 6" strokeOpacity=".55" />
      <path d="M174 122C186 122 186 99 199 99S212 145 225 145S238 99 251 99S264 145 277 145S290 99 303 99S316 145 329 145S342 99 355 99S368 122 381 122H402" stroke="#168A9B" strokeWidth="1.15" strokeOpacity=".22" transform="translate(0 8)" />
      <path d="M174 122C186 122 186 86 199 86S212 158 225 158S238 86 251 86S264 158 277 158S290 86 303 86S316 158 329 158S342 86 355 86S368 122 381 122H410" stroke={paint("signal")} strokeWidth="2.65" strokeLinecap="round" />
      <circle cx="251" cy="86" r="4" fill="#F8F9F8" stroke="#2B5ED7" strokeWidth="1.5" />
      <circle cx="329" cy="158" r="3.5" fill="#DC946A" stroke="#FAF8F4" strokeWidth="2" />
      <path d="M393 118L400 122L393 126" stroke="#168A9B" strokeOpacity=".7" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" />

      <ellipse cx="482" cy="201" rx="69" ry="9" fill="#DCE6F1" fillOpacity=".35" />
      <circle cx="482" cy="122" r="76" stroke="#B8CEDB" strokeOpacity=".3" strokeDasharray="2 7" />
      <circle cx="482" cy="122" r="65" fill={paint("disc")} stroke="#BBCFDE" strokeWidth="1.1" />
      <circle cx="482" cy="122" r="59" stroke="#FFFFFF" strokeOpacity=".9" />
      <g stroke="#A0B9C9" strokeWidth=".65">
        <path d="M433 122H531M482 73V171" strokeOpacity=".7" />
        <path d="M446 78V166M470 73V171M494 73V171M518 78V166M438 86H526M429 110H535M429 134H535M438 158H526" strokeOpacity=".17" />
        <path d="M528 119L531 122L528 125M479 76L482 73L485 76" strokeOpacity=".7" strokeLinecap="round" strokeLinejoin="round" />
      </g>
      <g fill="#168A9B">
        {symbols.flatMap((x) =>
          symbols.map((y) => (
            <circle key={`${x}-${y}`} cx={482 + x} cy={122 + y} r="2.65" opacity={x === 12 && y === -12 ? 0 : .72} />
          )),
        )}
      </g>
      <circle cx="494" cy="110" r="7" fill="#DC946A" fillOpacity=".12" />
      <circle cx="494" cy="110" r="3.3" fill="#DC946A" />
      <g fill="#7895A8" fontSize="9" fontFamily="ui-monospace, SFMono-Regular, Consolas, monospace">
        <text x="535" y="125">I</text>
        <text x="479" y="69">Q</text>
      </g>

      <g fontFamily="ui-monospace, SFMono-Regular, Consolas, monospace" fontSize="10" letterSpacing="1.5">
        <rect x="96" y="222" width="37" height="20" rx="10" fill="#E7EEF9" />
        <text x="115" y="235.5" fill="#5175B5" textAnchor="middle">Tx</text>
        <rect x="463" y="222" width="37" height="20" rx="10" fill="#E2EFF0" />
        <text x="482" y="235.5" fill="#3F818D" textAnchor="middle">Rx</text>
      </g>

      {!compact && (
        <g className="signal-artwork-details">
          <path d="M284 49H298M291 42V56M564 167H574M569 162V172" stroke="#9CB6CA" strokeWidth=".9" strokeLinecap="round" opacity=".6" />
          <circle cx="191" cy="194" r="2" fill="#DC946A" opacity=".65" />
          <circle cx="545" cy="49" r="3" stroke="#A6BDCF" strokeWidth=".8" />
          <path d="M268 198H350" stroke="#C6D4E0" strokeWidth=".8" />
          <path d="M268 195V201M309 196V200M350 195V201" stroke="#B5C7D8" strokeWidth=".8" />
        </g>
      )}
    </svg>
  );
}
