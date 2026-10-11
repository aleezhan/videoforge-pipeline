import { Navigate, Route, Routes } from "react-router";
import Layout from "@/components/Layout";
import Accounts from "@/pages/Accounts";
import AccountDetail from "@/pages/AccountDetail";
import Projects from "@/pages/Projects";
import ProjectDetail from "@/pages/ProjectDetail";
import EpisodeDetail from "@/pages/EpisodeDetail";
import Tests from "@/pages/Tests";
import Resources from "@/pages/Resources";
import ResourceKind from "@/pages/ResourceKind";
import ResourceDetail from "@/pages/ResourceDetail";
import Library from "@/pages/Library";
import LibraryDecision from "@/pages/LibraryDecision";
import TechniqueDetail from "@/pages/TechniqueDetail";
import Bench from "@/pages/Bench";
import BenchTest from "@/pages/BenchTest";
import BenchFile from "@/pages/BenchFile";
import BenchDetail from "@/pages/BenchDetail";
import Capabilities from "@/pages/Capabilities";
import Plans from "@/pages/Plans";
import FeatureDetail from "@/pages/FeatureDetail";
import Links from "@/pages/Links";
import Inbox from "@/pages/Inbox";
import LinkDetail from "@/pages/LinkDetail";
import AgentTasks from "@/pages/AgentTasks";

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Navigate to="/projects" replace />} />
        <Route path="/accounts" element={<Accounts />} />
        <Route path="/accounts/:id" element={<AccountDetail />} />
        <Route path="/projects" element={<Projects />} />
        <Route path="/projects/:id" element={<ProjectDetail />} />
        <Route path="/projects/:id/episodes/:ep" element={<EpisodeDetail />} />
        <Route path="/tests" element={<Tests />} />
        <Route path="/resources" element={<Resources />} />
        <Route path="/resources/:kind" element={<ResourceKind />} />
        <Route path="/resources/:kind/:id" element={<ResourceDetail />} />
        <Route path="/library" element={<Library />} />
        <Route path="/library/:decision" element={<LibraryDecision />} />
        <Route path="/library/:decision/:id" element={<TechniqueDetail />} />
        <Route path="/bench" element={<Bench />} />
        <Route path="/bench/tests/:id" element={<BenchTest />} />
        <Route path="/bench/file/*" element={<BenchFile />} />
        <Route path="/bench/:id" element={<BenchDetail />} />
        <Route path="/capabilities" element={<Capabilities />} />
        <Route path="/plans" element={<Plans />} />
        <Route path="/features/:id" element={<FeatureDetail />} />
        <Route path="/links" element={<Links />} />
        <Route path="/links/:id" element={<LinkDetail />} />
        <Route path="/inbox" element={<Inbox />} />
        <Route path="/agent-tasks" element={<AgentTasks />} />
        <Route path="*" element={<Navigate to="/projects" replace />} />
      </Route>
    </Routes>
  );
}
