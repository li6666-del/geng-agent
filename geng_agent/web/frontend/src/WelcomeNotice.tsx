import { useEffect, useRef } from "react";
import { Heart, Mail, Radio, X } from "lucide-react";
import "./WelcomeNotice.css";

export type WelcomeNoticeProps = {
  open: boolean;
  onDismiss: () => void;
  onAcknowledge: () => void;
};

export function WelcomeNotice({ open, onDismiss, onAcknowledge }: WelcomeNoticeProps) {
  const dialog = useRef<HTMLDialogElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const body = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const element = dialog.current;
    if (!open || !element) return;

    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    if (!element.open) element.showModal();
    if (body.current) body.current.scrollTop = 0;
    heading.current?.focus({ preventScroll: true });

    return () => {
      if (element.open) element.close();
      document.body.style.overflow = previousOverflow;
    };
  }, [open]);

  return <dialog
    className="welcome-dialog"
    ref={dialog}
    aria-labelledby="welcome-title"
    onCancel={event => { event.preventDefault(); onDismiss(); }}
  >
    <header className="welcome-header">
      <p className="welcome-eyebrow"><Radio size={16} aria-hidden="true" /> 一封写给通信人的信</p>
      <h2 id="welcome-title" ref={heading} tabIndex={-1}>亲爱的通信人，你好！</h2>
      <button className="welcome-close" type="button" aria-label="关闭使用说明" onClick={onDismiss}><X size={20} aria-hidden="true" /></button>
    </header>

    <div className="welcome-body" ref={body}>
      <section className="welcome-section">
        <p>耿同学 agent 是我们大创团队研制的面向通信论文本地复现与可信审查的多智能体系统，目标是对您输入的一篇通信论文进行复现，最终返回给您报告与代码。</p>
      </section>

      <section className="welcome-section" aria-labelledby="welcome-architecture">
        <h3 id="welcome-architecture">智能体如何协作</h3>
        <ol className="welcome-agents">
          <li><strong>论文理解智能体</strong><p>对论文提取事实。</p></li>
          <li><strong>架构设计智能体</strong><p>设计多个复现任务。</p></li>
          <li><strong>任务复现智能体</strong><p>根据事实与任务尝试复现论文，对论文中没有的参数会做出最合理的假设。</p></li>
          <li><strong>复现评价智能体</strong><p>以论文为参照，独立审查本地复现效果。</p></li>
          <li><strong>报告编辑智能体</strong><p>负责起草编写报告。</p></li>
        </ol>
      </section>

      <section className="welcome-section welcome-reminder" aria-labelledby="welcome-limits">
        <h3 id="welcome-limits">需要您知道的是</h3>
        <p>由于论文必定会缺少部分参数，且目前 LLM 并非全知全能的神，最终的本地复现旨在帮助您更好理解论文。</p>
        <p>由于目前硬件限制，请谨慎上传在复现过程中需要大规模并行运算的论文。</p>
      </section>

      <section className="welcome-section" aria-labelledby="welcome-model">
        <h3 id="welcome-model">关于模型与使用成本</h3>
        <p>智能体使用的 LLM 目前均为 <strong>DeepSeek V4.1 Flash</strong>，产生的 token 消耗由本人承担。</p>
      </section>

      <section className="welcome-section welcome-contact" aria-labelledby="welcome-feedback">
        <h3 id="welcome-feedback"><Heart size={17} aria-hidden="true" /> 期待您的反馈</h3>
        <p>您需要做的，是在获得报告与代码后对复现效果进行评估，并将任何您觉得有必要的建议发到我的邮箱：</p>
        <a href="mailto:lisongjun020@gmail.com"><Mail size={16} aria-hidden="true" /> lisongjun020@gmail.com</a>
      </section>
    </div>

    <footer className="welcome-footer">
      <p>可随时从页面右上角的“使用说明”重新查看。</p>
      <button className="button primary" type="button" onClick={onAcknowledge}>我已了解，开始使用</button>
    </footer>
  </dialog>;
}

export default WelcomeNotice;
