import Foundation
import Vision
import AppKit
var output: [[String: Any]] = []
for filename in CommandLine.arguments.dropFirst() {
    guard let image = NSImage(contentsOfFile: filename), let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else { continue }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["zh-Hans", "en-US"]
    request.usesLanguageCorrection = false
    try VNImageRequestHandler(cgImage: cg, options: [:]).perform([request])
    let lines: [[String: Any]] = (request.results ?? []).compactMap { observation in
        guard let text = observation.topCandidates(1).first else { return nil }
        let box = observation.boundingBox
        return ["text": text.string, "x": box.minX, "y": 1 - box.maxY, "w": box.width, "h": box.height]
    }
    output.append(["file": filename, "width": cg.width, "height": cg.height, "lines": lines])
}
let bytes = try JSONSerialization.data(withJSONObject: output, options: [.prettyPrinted, .sortedKeys])
FileHandle.standardOutput.write(bytes)
