import { useMemo, useRef, useState } from "react";
import { Canvas, useFrame } from "@react-three/fiber";
import {
  Environment,
  Lightformer,
  PresentationControls,
} from "@react-three/drei";
import { ParametricGeometry } from "three/addons/geometries/ParametricGeometry.js";
import * as THREE from "three";

/**
 * The one 3D element in the app: a chrome ribbon wound into a sphere.
 *
 * It turns slowly on its own, can be dragged to spin, and leans toward the
 * cursor while hovered. Lighting is procedural, so nothing is downloaded.
 */

const TURNS = 4.5;
// How much of the gap between two turns the ribbon covers.
const RIBBON_FILL = 0.78;
// How far the middle of the ribbon bulges out, giving it a rounded profile.
const BULGE = 0.07;

function spiralRibbon(u: number, v: number, target: THREE.Vector3) {
  // u runs along the ribbon from pole to pole, v across its width.
  const across = v * 2 - 1;
  const half = (Math.PI / TURNS) * RIBBON_FILL * 0.5;
  const latitude = THREE.MathUtils.clamp(
    -Math.PI / 2 + Math.PI * u + across * half,
    -Math.PI / 2,
    Math.PI / 2,
  );
  const longitude = Math.PI * 2 * TURNS * u;
  const radius = 1 + BULGE * Math.sqrt(1 - across * across);

  target.set(
    radius * Math.cos(latitude) * Math.cos(longitude),
    radius * Math.sin(latitude),
    radius * Math.cos(latitude) * Math.sin(longitude),
  );
}

function Ribbon() {
  const groupRef = useRef<THREE.Group>(null);
  const [hovered, setHovered] = useState(false);
  const geometry = useMemo(
    () => new ParametricGeometry(spiralRibbon, 720, 14),
    [],
  );

  useFrame((state, delta) => {
    const group = groupRef.current;
    if (!group) return;

    group.rotation.y += delta * (hovered ? 0.45 : 0.18);

    const leanX = hovered ? -state.pointer.y * 0.35 : 0;
    const leanZ = hovered ? -state.pointer.x * 0.2 : 0;
    group.rotation.x = THREE.MathUtils.damp(group.rotation.x, 0.55 + leanX, 3, delta);
    group.rotation.z = THREE.MathUtils.damp(group.rotation.z, -0.45 + leanZ, 3, delta);

    const scale = hovered ? 1.04 : 1;
    group.scale.setScalar(THREE.MathUtils.damp(group.scale.x, scale, 6, delta));
  });

  return (
    <group
      ref={groupRef}
      onPointerOver={() => setHovered(true)}
      onPointerOut={() => setHovered(false)}
    >
      <mesh geometry={geometry}>
        <meshPhysicalMaterial
          color="#f1f4f9"
          metalness={1}
          roughness={0.07}
          clearcoat={1}
          clearcoatRoughness={0.05}
          side={THREE.DoubleSide}
        />
      </mesh>
      {/* Dark core seen through the gaps between turns. */}
      <mesh scale={0.9}>
        <sphereGeometry args={[1, 48, 48]} />
        <meshStandardMaterial color="#050608" metalness={0.6} roughness={0.5} />
      </mesh>
    </group>
  );
}

function Studio() {
  return (
    <Environment resolution={256} frames={1}>
      <color attach="background" args={["#24272e"]} />
      <Lightformer form="rect" intensity={6} color="#ffffff" position={[0, 6, 0]} rotation-x={Math.PI / 2} scale={[10, 3, 1]} />
      <Lightformer form="rect" intensity={4} color="#ffffff" position={[0, 1.2, 9]} rotation-y={Math.PI} scale={[16, 1.4, 1]} />
      <Lightformer form="rect" intensity={2} color="#ffffff" position={[0, -1.6, 9]} rotation-y={Math.PI} scale={[16, 0.5, 1]} />
      <Lightformer form="rect" intensity={3} color="#e4e9ff" position={[-7, 1, 2]} rotation-y={Math.PI / 2} scale={[2, 10, 1]} />
      <Lightformer form="rect" intensity={2.5} color="#ffffff" position={[7, -1, 2]} rotation-y={-Math.PI / 2} scale={[2, 10, 1]} />
      <Lightformer form="rect" intensity={1.5} color="#ffffff" position={[0, 0, -8]} scale={[10, 2, 1]} />
      <mesh position={[0, -7, 0]} rotation-x={-Math.PI / 2}>
        <planeGeometry args={[40, 40]} />
        <meshBasicMaterial color="#050506" side={THREE.DoubleSide} />
      </mesh>
    </Environment>
  );
}

export default function ChromeSphere() {
  return (
    <div className="chrome-sphere" title="Drag to spin">
      <Canvas camera={{ position: [0, 0, 4.4], fov: 38 }} dpr={[1, 2]} gl={{ alpha: true }}>
        <Studio />
        <PresentationControls
          global={false}
          cursor
          snap
          speed={1.4}
          polar={[-Math.PI / 4, Math.PI / 4]}
          azimuth={[-Infinity, Infinity]}
        >
          <Ribbon />
        </PresentationControls>
      </Canvas>
    </div>
  );
}
