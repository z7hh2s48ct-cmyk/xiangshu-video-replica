import "./oral.css";

export function OralJourney({ step }: { step: 1 | 2 | 3 }) {
  return (
    <ol className="oral-journey" aria-label="数字人口播制作流程">
      {["视频分身", "克隆声音", "文案成片"].map((label, index) => (
        <li key={label} aria-current={step === index + 1 ? "step" : undefined}>
          <span>0{index + 1}</span>
          <strong>{label}</strong>
          <small>
            {
              [
                "上传真人视频，创建分身",
                "上传声音样本，试听确认",
                "输入文案，用自己的声音讲述",
              ][index]
            }
          </small>
        </li>
      ))}
    </ol>
  );
}
