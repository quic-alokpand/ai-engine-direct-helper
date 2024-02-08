//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "QNNHelper.h"

#define QNNHELPER_VERSION    "2.19.0"


ShareMemory::ShareMemory(const std::string& share_memory_name, const size_t share_memory_size) {
    m_share_memory_name = share_memory_name;
    g_LibQNNHelper.CreateShareMemory(share_memory_name, share_memory_size);
}

ShareMemory::~ShareMemory() {
    g_LibQNNHelper.DeleteShareMemory(m_share_memory_name);
}


QNNContext::QNNContext(const std::string& model_name,
                       const std::string& model_path, const std::string& backend_lib_path, const std::string& system_lib_path) {
    m_model_name = model_name;
    g_LibQNNHelper.ModelInitialize(model_name, model_path, backend_lib_path, system_lib_path);
}

QNNContext::QNNContext(const std::string& model_name, const std::string& proc_name,
                       const std::string& model_path, const std::string& backend_lib_path, const std::string& system_lib_path) {
    m_model_name = model_name;
    m_proc_name = proc_name;
    g_LibQNNHelper.ModelInitialize(model_name, proc_name, model_path, backend_lib_path, system_lib_path);
}

QNNContext::~QNNContext() {
    if (m_proc_name.empty())
        g_LibQNNHelper.ModelDestroy(m_model_name);
    else
        g_LibQNNHelper.ModelDestroy(m_model_name, m_proc_name);
}

std::vector<py::array_t<float>> 
QNNContext::Inference(const std::vector<py::array_t<float>>& input, const std::string& perf_profile) {
    return inference(m_model_name, input, perf_profile);
}

std::vector<py::array_t<float>> 
QNNContext::Inference(const ShareMemory& share_memory, const std::vector<py::array_t<float>>& input, const std::string& perf_profile) {
    return inference_P(m_model_name, m_proc_name, share_memory.m_share_memory_name, input, perf_profile);
}


PYBIND11_MODULE(pyqnnhelper, m) {
    m.doc() = R"pbdoc(
        Pybind11 QNNHelper Extension.
        -----------------------
        .. currentmodule:: qnnhelper
        .. autosummary::
            :toctree: _generate

            model_initialize
            model_inference
            model_destroy
            memory_create
            memory_delete
            set_log_level
            set_profiling_level
            set_perf_profile
            rel_perf_profile
            )pbdoc";

    m.attr("__name__") = "qnnhelper";
    m.attr("__version__") = QNNHELPER_VERSION;
    m.attr("__author__") = "quic-zhanweiw";
    m.attr("__license__") = "BSD-3-Clause";

    m.def("model_initialize", &initialize, "Initialize models.");
    m.def("model_initialize", &initialize_P, "Initialize models.");
    m.def("model_inference", &inference, "Inference models.");
    m.def("model_inference", &inference_P, "Inference models.");
    m.def("model_destroy", &destroy, "Destroy models.");
    m.def("model_destroy", &destroy_P, "Destroy models.");
    m.def("memory_create", &create_memory, "Create share memory.");
    m.def("memory_delete", &delete_memory, "Delete share memory.");
    m.def("set_log_level", &set_log_level, "Set QNN log level.");
    m.def("set_profiling_level", &set_profiling_level, "Set QNN profiling level.");
    m.def("set_perf_profile", &set_perf_profile, "Set HTP perf profile.");
    m.def("rel_perf_profile", &rel_perf_profile, "Release HTP perf profile.");


    py::class_<ShareMemory>(m, "ShareMemory")
        .def(py::init<const std::string&, const size_t>());
    
    py::class_<QNNContext>(m, "QNNContext")
        .def(py::init<const std::string&, const std::string&, const std::string&, const std::string&>())
        .def(py::init<const std::string&, const std::string&, const std::string&, const std::string&, const std::string&>())
        .def("Inference", py::overload_cast<const std::vector<py::array_t<float>>&, const std::string&>(&QNNContext::Inference))
        .def("Inference", py::overload_cast<const ShareMemory&, const std::vector<py::array_t<float>>&, const std::string&>(&QNNContext::Inference));

}

