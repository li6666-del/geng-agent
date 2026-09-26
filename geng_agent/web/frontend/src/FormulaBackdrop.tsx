import "./FormulaBackdrop.css";

/** Familiar channel equations used only as quiet page-edge decoration. */
export default function FormulaBackdrop() {
  return (
    <div className="formula-backdrop" aria-hidden="true">
      <div className="formula-note formula-note--capacity">
        <span className="formula-note__label">CHANNEL CAPACITY</span>
        <span className="formula-note__equation">
          <i>C</i> = <i>B</i> log<sub>2</sub>
          <span className="formula-note__continuation">(1 + <i>S</i>/<i>N</i>)</span>
        </span>
      </div>
      <div className="formula-note formula-note--channel">
        <span className="formula-note__label">MIMO CHANNEL</span>
        <span className="formula-note__equation">
          <b>y</b> = <b>H</b><b>x</b> + <b>n</b>
        </span>
      </div>
      <div className="formula-note formula-note--ber">
        <span className="formula-note__label">BPSK · AWGN</span>
        <span className="formula-note__equation">
          <i>P</i><sub>b</sub> = <i>Q</i>
          <span className="formula-note__continuation">(√(2<i>E</i><sub>b</sub>/<i>N</i><sub>0</sub>))</span>
        </span>
      </div>
      <div className="formula-note formula-note--multipath">
        <span className="formula-note__label">MULTIPATH</span>
        <span className="formula-note__equation">
          <i>h</i>(τ) = ∑<sub>ℓ</sub> <i>α</i><sub>ℓ</sub>
          <span className="formula-note__continuation">δ(τ − τ<sub>ℓ</sub>)</span>
        </span>
      </div>
      <div className="formula-note formula-note--ofdm">
        <span className="formula-note__label">ORTHOGONALITY</span>
        <span className="formula-note__equation">
          Δ<i>f</i> = 1/<i>T</i><sub>u</sub>
        </span>
      </div>
      <div className="formula-note formula-note--carrier">
        <span className="formula-note__label">CARRIER WAVE</span>
        <span className="formula-note__equation">
          <i>s</i>(<i>t</i>) = <i>A</i> cos
          <span className="formula-note__continuation">(2π<i>f</i><sub>c</sub><i>t</i> + φ)</span>
        </span>
      </div>
    </div>
  );
}
