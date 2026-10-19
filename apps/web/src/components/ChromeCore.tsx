import { useRef, useState, type ComponentRef } from "react";
import { Canvas, useFrame } from "@react-three/fiber";
import { MeshDistortMaterial, PresentationControls } from "@react-three/drei";
import * as THREE from "three";

import { ChromeStudio } from "./ChromeBackdrop";

/**
 * Interactive chrome core for the Overview hero.
 *
 * Drag to turn it, hover to make the liquid chrome ripple, click to send a
 * pulse through the orbit rings.
 */

function Core() {
  const coreRef = useRef<THREE.Mesh>(null);
  const ringsRef = useRef<THREE.Group>(null);
  const distortRef = useRef<ComponentRef<typeof MeshDistortMaterial>>(null);
  const pulse = useRef(0);
  const [hovered, setHovered] = useState(false);

  useFrame((state, delta) => {
    pulse.current = Math.max(0, pulse.current - delta * 1.4);
    const p = pulse.current;

    const core = coreRef.current;
    if (core) {
      const target = (hovered ? 1.08 : 1) + p * 0.25;
      core.scale.setScalar(THREE.MathUtils.damp(core.scale.x, target, 8, delta));
      core.rotation.y += delta * (0.25 + p * 3);
    }

    const material = distortRef.current;
    if (material) {
      material.distort = THREE.MathUtils.damp(
        material.distort,
        (hovered ? 0.42 : 0.22) + p * 0.35,
        4,
        delta,
      );
    }

    const rings = ringsRef.current;
    if (rings) {
      rings.children.forEach((ring, index) => {
        const direction = index % 2 === 0 ? 1 : -1;
        ring.rotation.z += delta * direction * (0.35 + index * 0.15 + p * 6);
        ring.rotation.x =
          Math.PI / 2.4 + Math.sin(state.clock.elapsedTime * 0.5 + index) * 0.15;
      });
    }
  });

  return (
    <group>
      <mesh
        ref={coreRef}
        onPointerOver={(event) => {
          event.stopPropagation();
          setHovered(true);
        }}
        onPointerOut={() => setHovered(false)}
        onClick={(event) => {
          event.stopPropagation();
          pulse.current = 1;
        }}
      >
        <sphereGeometry args={[1, 128, 128]} />
        <MeshDistortMaterial
          ref={distortRef}
          color="#eef3fb"
          metalness={1}
          roughness={0.06}
          distort={0.22}
          speed={hovered ? 3.2 : 1.6}
        />
      </mesh>

      <group ref={ringsRef}>
        {[1.55, 1.9, 2.25].map((radius, index) => (
          <mesh key={radius} rotation={[Math.PI / 2.4, index * 0.6, 0]}>
            <torusGeometry args={[radius, 0.025 + index * 0.008, 24, 160]} />
            <meshPhysicalMaterial
              color={index === 1 ? "#b9c8ff" : "#f2f5fb"}
              metalness={1}
              roughness={0.12}
              clearcoat={1}
            />
          </mesh>
        ))}
      </group>
    </group>
  );
}

export default function ChromeCore() {
  return (
    <div className="chrome-core" title="Drag to rotate, click to pulse">
      <Canvas camera={{ position: [0, 0, 7.4], fov: 40 }} dpr={[1, 2]} gl={{ alpha: true }}>
        <ChromeStudio />
        <PresentationControls
          global={false}
          cursor
          snap
          speed={1.6}
          rotation={[0.15, -0.3, 0]}
          polar={[-Math.PI / 3, Math.PI / 3]}
          azimuth={[-Infinity, Infinity]}
        >
          <Core />
        </PresentationControls>
      </Canvas>
      <span className="chrome-core-hint">Drag · Click</span>
    </div>
  );
}
