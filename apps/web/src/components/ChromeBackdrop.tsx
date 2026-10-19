import { useEffect, useMemo, useRef, type ReactNode } from "react";
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { Environment, Lightformer } from "@react-three/drei";
import * as THREE from "three";

/**
 * Full-screen 3D scene that sits behind the glass workspace.
 *
 * The canvas itself never takes pointer events, so the UI on top stays
 * clickable. Instead, the cursor position is read from the window: the
 * whole scene leans toward it, and nearby shapes are pushed away from it.
 */

type ShapeKind = "torusKnot" | "torus" | "icosahedron" | "sphere" | "capsule";

interface ShapeSpec {
  kind: ShapeKind;
  position: [number, number, number];
  scale: number;
  material: "chrome" | "glass";
  spin: [number, number];
}

const SHAPES: ShapeSpec[] = [
  { kind: "torusKnot", position: [4.6, 1.6, -1.5], scale: 0.9, material: "chrome", spin: [0.18, 0.24] },
  { kind: "icosahedron", position: [-5.2, -1.4, -2], scale: 1.05, material: "glass", spin: [0.12, -0.1] },
  { kind: "torus", position: [-6.4, 2.2, -2], scale: 0.85, material: "chrome", spin: [-0.2, 0.14] },
  { kind: "sphere", position: [2.4, -2.5, -0.6], scale: 0.62, material: "chrome", spin: [0, 0.1] },
  { kind: "capsule", position: [6.3, -1.1, -3.2], scale: 0.7, material: "glass", spin: [0.25, 0.08] },
  { kind: "sphere", position: [6.2, 3.9, -5], scale: 0.42, material: "glass", spin: [0, 0.2] },
  { kind: "icosahedron", position: [0.4, -3.6, -3.8], scale: 0.5, material: "chrome", spin: [0.3, 0.2] },
];

// Cursor in normalized device coordinates (-1..1), shared by the scene.
const pointer = new THREE.Vector2(0, 0);
let pointerActive = false;

function ShapeGeometry({ kind }: { kind: ShapeKind }) {
  switch (kind) {
    case "torusKnot":
      return <torusKnotGeometry args={[0.8, 0.26, 220, 32]} />;
    case "torus":
      return <torusGeometry args={[0.9, 0.3, 48, 120]} />;
    case "icosahedron":
      return <icosahedronGeometry args={[1, 0]} />;
    case "capsule":
      return <capsuleGeometry args={[0.45, 1.1, 16, 32]} />;
    default:
      return <sphereGeometry args={[1, 64, 64]} />;
  }
}

function FloatingShape({ spec, index }: { spec: ShapeSpec; index: number }) {
  const meshRef = useRef<THREE.Mesh>(null);
  const base = useMemo(() => new THREE.Vector3(...spec.position), [spec]);
  const cursorWorld = useMemo(() => new THREE.Vector3(), []);
  const push = useMemo(() => new THREE.Vector3(), []);
  const camera = useThree((state) => state.camera);

  useFrame((state, delta) => {
    const mesh = meshRef.current;
    if (!mesh) return;

    const t = state.clock.elapsedTime + index * 1.7;

    // Project the cursor onto the plane this shape floats in.
    cursorWorld.set(pointer.x, pointer.y, 0.5).unproject(camera);
    cursorWorld.sub(camera.position).normalize();
    const distance = (base.z - camera.position.z) / cursorWorld.z;
    cursorWorld.multiplyScalar(distance).add(camera.position);

    push.set(0, 0, 0);
    if (pointerActive) {
      push.subVectors(base, cursorWorld);
      push.z = 0;
      const gap = push.length();
      const radius = 2.6;
      if (gap < radius) {
        push.normalize().multiplyScalar((radius - gap) * 0.55);
      } else {
        push.set(0, 0, 0);
      }
    }

    const targetX = base.x + push.x + Math.sin(t * 0.4) * 0.15;
    const targetY = base.y + push.y + Math.sin(t * 0.6) * 0.25;

    mesh.position.x = THREE.MathUtils.damp(mesh.position.x, targetX, 3, delta);
    mesh.position.y = THREE.MathUtils.damp(mesh.position.y, targetY, 3, delta);

    // Shapes spin faster while the cursor is pushing them.
    const boost = 1 + push.length() * 2.2;
    mesh.rotation.x += delta * spec.spin[0] * boost;
    mesh.rotation.y += delta * spec.spin[1] * boost;
  });

  return (
    <mesh ref={meshRef} position={spec.position} scale={spec.scale}>
      <ShapeGeometry kind={spec.kind} />
      {spec.material === "chrome" ? (
        <meshPhysicalMaterial
          color="#e8eef8"
          metalness={1}
          roughness={0.08}
          clearcoat={1}
          clearcoatRoughness={0.04}
        />
      ) : (
        <meshPhysicalMaterial
          color="#cfe0ff"
          metalness={0.1}
          roughness={0.05}
          transmission={1}
          thickness={1.2}
          ior={1.45}
          iridescence={0.6}
          iridescenceIOR={1.3}
          clearcoat={1}
          transparent
        />
      )}
    </mesh>
  );
}

function Rig({ children }: { children: ReactNode }) {
  const groupRef = useRef<THREE.Group>(null);

  useFrame((_, delta) => {
    const group = groupRef.current;
    if (!group) return;

    group.rotation.y = THREE.MathUtils.damp(group.rotation.y, pointer.x * 0.18, 2, delta);
    group.rotation.x = THREE.MathUtils.damp(group.rotation.x, -pointer.y * 0.12, 2, delta);
  });

  return <group ref={groupRef}>{children}</group>;
}

export function ChromeStudio() {
  // Procedural studio lighting, so no HDR file has to be downloaded.
  return (
    <Environment resolution={256} frames={1}>
      <color attach="background" args={["#7c8598"]} />
      {/* Bands behind the camera: what the face of a chrome object mirrors. */}
      <Lightformer form="rect" intensity={3} color="#eef2ff" position={[0, 1.2, 10]} rotation-y={Math.PI} scale={[18, 2.2, 1]} />
      <mesh position={[0, -1.6, 10]} rotation-y={Math.PI}>
        <planeGeometry args={[30, 3]} />
        <meshBasicMaterial color="#0b0e15" side={THREE.DoubleSide} />
      </mesh>
      <mesh position={[0, -9, 0]} rotation-x={-Math.PI / 2}>
        <planeGeometry args={[40, 40]} />
        <meshBasicMaterial color="#1a1e28" side={THREE.DoubleSide} />
      </mesh>
      <Lightformer form="rect" intensity={6} color="#ffffff" position={[0, 6, -2]} rotation-x={Math.PI / 2} scale={[14, 6, 1]} />
      <Lightformer form="rect" intensity={3} color="#ffffff" position={[0, 0, -8]} scale={[16, 1.2, 1]} />
      <Lightformer form="rect" intensity={4} color="#a9bbff" position={[-7, 1, 1]} rotation-y={Math.PI / 2} scale={[10, 4, 1]} />
      <Lightformer form="rect" intensity={4} color="#ffd2f2" position={[7, -1, 1]} rotation-y={-Math.PI / 2} scale={[10, 4, 1]} />
      <Lightformer form="rect" intensity={2} color="#ffffff" position={[0, -5, 3]} rotation-x={-Math.PI / 2} scale={[12, 3, 1]} />
      <Lightformer form="ring" intensity={4} color="#7ee3ff" position={[-3, -3, 6]} scale={3} />
      <Lightformer form="circle" intensity={8} color="#ffffff" position={[3, 3, 7]} scale={2} />
    </Environment>
  );
}

export default function ChromeBackdrop() {
  useEffect(() => {
    function handleMove(event: PointerEvent) {
      pointer.set(
        (event.clientX / window.innerWidth) * 2 - 1,
        -(event.clientY / window.innerHeight) * 2 + 1,
      );
      pointerActive = true;
    }

    function handleLeave() {
      pointerActive = false;
    }

    window.addEventListener("pointermove", handleMove);
    document.addEventListener("pointerleave", handleLeave);

    return () => {
      window.removeEventListener("pointermove", handleMove);
      document.removeEventListener("pointerleave", handleLeave);
    };
  }, []);

  return (
    <div className="chrome-backdrop" aria-hidden="true">
      <Canvas
        camera={{ position: [0, 0, 9], fov: 45 }}
        dpr={[1, 1.5]}
        gl={{ antialias: true, alpha: true }}
      >
        <ChromeStudio />
        <Rig>
          {SHAPES.map((spec, index) => (
            <FloatingShape key={index} spec={spec} index={index} />
          ))}
        </Rig>
      </Canvas>
    </div>
  );
}
