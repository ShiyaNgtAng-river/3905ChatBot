import Foundation
import Vision
import ImageIO

// No network, Apple ID, microphone, or photo-library permission is needed.
do {
    guard CommandLine.arguments.count == 2 else {
        throw NSError(domain: "GroupMedia", code: 1)
    }
    let url = URL(fileURLWithPath: CommandLine.arguments[1])
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["zh-Hans", "zh-Hant", "en-US"]
    request.usesLanguageCorrection = true
    let handler = VNImageRequestHandler(url: url, options: [:])
    try handler.perform([request])
    let lines: [[String: Any]] = (request.results ?? []).compactMap { result in
        guard let candidate = result.topCandidates(1).first else { return nil }
        let b = result.boundingBox
        return ["text": candidate.string, "confidence": candidate.confidence,
                "box": [b.origin.x, b.origin.y, b.size.width, b.size.height]]
    }
    let output: [String: Any] = ["text": lines.compactMap { $0["text"] as? String }.joined(separator: "\n"),
                                 "lines": lines, "box_coordinates": "normalized_bottom_left_xywh"]
    let bytes = try JSONSerialization.data(withJSONObject: output, options: [.sortedKeys])
    FileHandle.standardOutput.write(bytes)
} catch {
    // Do not emit paths or image contents in diagnostic errors.
    FileHandle.standardError.write(Data("Local OCR failed\n".utf8))
    exit(1)
}
