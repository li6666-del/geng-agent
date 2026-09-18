import { useEffect, useRef, type ReactNode } from "react";
import { FolderOpen, Radio, X } from "lucide-react";

export function navigate(path: string) { history.pushState({}, "", path); window.dispatchEvent(new PopStateEvent("popstate")); window.scrollTo(0, 0); }
export function message(reason: unknown) { return reason instanceof Error ? reason.message : "操作失败，请稍后重试。"; }
export function Brand() { return <a className="brand" href="/" onClick={event => { event.preventDefault(); navigate("/"); }}><span className="brand-mark"><Radio size={21} /></span><strong>耿同学 <span>Agent</span></strong><small>论文复现工作台</small></a>; }
export function Badge({ value, children }: { value: string; children: ReactNode }) { return <span className={`badge tone-${value}`}><i />{children}</span>; }
export function ErrorNote({ children }: { children: ReactNode }) { return <div className="error-note" role="alert">{children}</div>; }
export function Empty({ children }: { children: ReactNode }) { return <div className="empty"><FolderOpen size={25} /><p>{children}</p></div>; }

export function Modal({ title, children, onClose, wide = false }: { title: string; children: ReactNode; onClose: () => void; wide?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const node = ref.current!; const previous = document.activeElement as HTMLElement;
    node.showModal(); const overflow = document.body.style.overflow; document.body.style.overflow = "hidden";
    return () => { node.close(); document.body.style.overflow = overflow; previous?.focus(); };
  }, []);
  return <dialog ref={ref} className={`modal ${wide ? "wide-modal" : ""}`} aria-label={title} onCancel={event => { event.preventDefault(); onClose(); }} onClick={event => {
    if (event.target !== ref.current) return;
    const rect = ref.current!.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) onClose();
  }}><header className="modal-heading"><h2>{title}</h2><button className="icon-button" aria-label="关闭窗口" onClick={onClose}><X size={20} /></button></header>{children}</dialog>;
}
