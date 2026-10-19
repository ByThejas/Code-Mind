import { useEffect } from "react";

/**
 * Gives every `.tilt` element a 3D tilt that follows the cursor, plus a
 * moving specular highlight. Uses one delegated listener for the whole page,
 * so cards rendered later (or by other pages) pick it up automatically.
 */
export function useGlassTilt() {
  useEffect(() => {
    const reduceMotion = window.matchMedia(
      "(prefers-reduced-motion: reduce)",
    ).matches;

    if (reduceMotion) return;

    let current: HTMLElement | null = null;

    function reset(element: HTMLElement) {
      element.style.setProperty("--rx", "0deg");
      element.style.setProperty("--ry", "0deg");
      element.style.setProperty("--glare", "0");
    }

    function handleMove(event: PointerEvent) {
      const target = (event.target as Element | null)?.closest<HTMLElement>(
        ".tilt",
      );

      if (current && current !== target) {
        reset(current);
      }

      current = target ?? null;
      if (!current) return;

      const rect = current.getBoundingClientRect();
      const x = (event.clientX - rect.left) / rect.width;
      const y = (event.clientY - rect.top) / rect.height;
      // Large panels tilt less than small cards.
      const strength = Math.min(9, 2400 / Math.max(rect.width, 1));

      current.style.setProperty("--rx", `${(0.5 - y) * strength}deg`);
      current.style.setProperty("--ry", `${(x - 0.5) * strength}deg`);
      current.style.setProperty("--mx", `${x * 100}%`);
      current.style.setProperty("--my", `${y * 100}%`);
      current.style.setProperty("--glare", "1");
    }

    function handleLeave() {
      if (current) reset(current);
      current = null;
    }

    document.addEventListener("pointermove", handleMove);
    document.addEventListener("pointerleave", handleLeave);

    return () => {
      document.removeEventListener("pointermove", handleMove);
      document.removeEventListener("pointerleave", handleLeave);
    };
  }, []);
}
